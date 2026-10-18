from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from src import ptd_reader
from src.c3_quality import as_of_date, connect_rates, quote_path, sql_list
from src.common import log, sql_str, write_json
from src.contracts import CONTEXT_COLS, EXTRACT_VERSION, OUTLIER_GROUP_COLS

OUTLIER_GROUP = ", ".join(f"coalesce({c}, '')" for c in OUTLIER_GROUP_COLS)
NETWORK_HINT = "upper(nullif(regexp_extract(business_name, '^\\s*([A-Za-z][A-Za-z /&-]{1,40}):', 1), ''))"


def ctx_key(prefix=""):
    return ", ".join(f"{prefix}{c}" for c in CONTEXT_COLS)


def network_label_sql(c4):
    """In-file network evidence per provider group / price. The CA file serves every Health Net CA network, so this is the only
    PPO signal inside it: a PPO label in the TIN business name, an HMO-only label, or a capitation arrangement (an HMO payment model)."""
    ppo = sql_str(c4.get("ppo_label_pattern", r"(?i)\bPPO\b"))
    non_ppo = sql_str(c4.get("non_ppo_label_pattern", r"(?i)\bHMO\b"))
    arr = sql_list([a.lower() for a in c4.get("non_ppo_arrangements", ["capitation"])])
    return f"""CASE WHEN lower(coalesce(r.negotiation_arrangement, '')) IN ({arr}) THEN 'non_ppo_capitation'
                    WHEN regexp_matches(coalesce(refs.business_name, ''), {non_ppo}) AND NOT regexp_matches(coalesce(refs.business_name, ''), {ppo}) THEN 'non_ppo_label'
                    WHEN regexp_matches(coalesce(refs.business_name, ''), {ppo}) THEN 'ppo_label'
                    WHEN {NETWORK_HINT.replace('business_name', 'refs.business_name')} IS NOT NULL THEN 'other_label'
                    ELSE 'unlabelled' END"""


def selection_sql(c3, c4, as_of):
    placeholders = ", ".join(str(float(p)) for p in c3["placeholder_rates"])
    usable = sql_list([t.lower() for t in c3["usable_rate_types"]])
    pref = [t.lower() for t in c4.get("rate_type_preference") or c3["usable_rate_types"]]
    type_rank = "CASE lower(negotiated_type) " + " ".join(f"WHEN {sql_str(t)} THEN {i}" for i, t in enumerate(pref)) + f" ELSE {len(pref)} END"
    fold, min_group = float(c3["outlier_fold"]), int(c3["outlier_min_group"])
    open_ended = sql_str(c3["open_ended_expiration"])
    exclude = bool(c4.get("exclude_non_ppo_evidence", True))
    key = ctx_key()
    return f"""
    CREATE TEMP TABLE flagged AS
    SELECT r.* REPLACE ({", ".join(f"coalesce(r.{c}, '') AS {c}" for c in CONTEXT_COLS if c not in ("npi", "tin"))}),
           refs.npi, coalesce(refs.tin, '') AS tin, refs.tin_type, refs.business_name, {NETWORK_HINT.replace('business_name', 'refs.business_name')} AS network_hint,
           {network_label_sql(c4)} AS network_label,
           {type_rank} AS type_rank,
           md5(concat_ws('|', {OUTLIER_GROUP})) AS grp
    FROM rates r JOIN refs USING (provider_group_id)
    WHERE refs.npi IS NOT NULL;

    ALTER TABLE flagged ADD COLUMN drop_reason VARCHAR;
    UPDATE flagged SET drop_reason =
           CASE WHEN {str(exclude).lower()} AND network_label LIKE 'non_ppo%' THEN 'non_ppo_evidence'
                WHEN negotiated_rate IS NULL OR negotiated_rate <= 0 OR negotiated_rate IN ({placeholders}) THEN 'invalid_rate'
                WHEN coalesce(expiration_date, '') NOT IN ('', {open_ended}) AND expiration_date < {sql_str(as_of.isoformat())} THEN 'expired'
                WHEN lower(coalesce(negotiated_type, '')) NOT IN ({usable}) THEN 'non_dollar_rate_type'
           END;

    CREATE TEMP TABLE medians AS
    SELECT grp, median(negotiated_rate) AS med FROM flagged WHERE negotiated_rate > 0 AND coalesce(drop_reason, '') <> 'non_ppo_evidence'
    GROUP BY grp HAVING count(*) >= {min_group};

    CREATE TEMP TABLE usable AS
    SELECT f.*, coalesce(f.negotiated_rate > m.med * {fold} OR f.negotiated_rate < m.med / {fold}, false) AS is_outlier, m.med AS group_median
    FROM flagged f LEFT JOIN medians m USING (grp) WHERE f.drop_reason IS NULL;

    CREATE TEMP TABLE ctx AS
    SELECT {key}, min(type_rank) AS best_type_rank, count(*) AS n_usable_rows, count(DISTINCT negotiated_rate) AS n_distinct_usable,
           min(negotiated_rate) AS rate_min, max(negotiated_rate) AS rate_max,
           bool_or(NOT is_outlier) AS has_non_outlier, count(DISTINCT negotiated_type) AS n_types,
           min(procedure_code) AS procedure_code, min(service_names) AS service_names, max(n_service_names) AS n_service_names,
           min(description) AS description, min(network_label) AS network_label
    FROM usable GROUP BY ALL;

    CREATE TEMP TABLE ctx_outliers AS
    SELECT {key}, count(*) FILTER (WHERE u.is_outlier AND u.type_rank = c.best_type_rank) AS n_outlier_rows
    FROM usable u JOIN ctx c USING ({key}) GROUP BY ALL;

    CREATE TEMP TABLE cand AS
    SELECT u.* FROM usable u JOIN ctx c USING ({key})
    WHERE u.type_rank = c.best_type_rank AND (NOT u.is_outlier OR NOT c.has_non_outlier);

    CREATE TEMP TABLE votes AS
    SELECT {key}, negotiated_type, negotiated_rate, count(*) AS support,
           string_agg(DISTINCT provider_group_id, '|' ORDER BY provider_group_id) AS provider_group_ids,
           min(tin_type) AS tin_type, min(network_hint) AS network_hint, min(group_median) AS group_median
    FROM cand GROUP BY ALL;

    CREATE TEMP TABLE ranked AS
    SELECT *, count(*) OVER w AS n_candidates,
           count(*) FILTER (WHERE support = top_support) OVER w AS n_tied
    FROM (SELECT *, max(support) OVER w AS top_support FROM votes WINDOW w AS (PARTITION BY {key})) v
    WINDOW w AS (PARTITION BY {key});

    CREATE TEMP TABLE tied AS
    SELECT *, row_number() OVER (PARTITION BY {key} ORDER BY negotiated_rate, negotiated_type) AS pos FROM ranked WHERE support = top_support;

    CREATE TEMP TABLE selected AS
    SELECT t.npi, nullif(t.tin, '') AS tin, t.tin_type, nullif(t.negotiation_arrangement, '') AS negotiation_arrangement,
           t.billing_code_type, t.billing_code, c.procedure_code, nullif(t.associated_code, '') AS associated_code,
           nullif(t.service_key, '') AS service_key, c.service_names AS name, c.n_service_names, c.description,
           nullif(t.billing_code_modifier, '') AS billing_code_modifier, nullif(t.billing_class, '') AS billing_class,
           nullif(t.setting, '') AS setting, nullif(t.service_code, '') AS service_code, t.negotiated_type, t.negotiated_rate,
           c.n_usable_rows AS n_candidate_rows, c.n_distinct_usable AS n_distinct_rates, c.rate_min, c.rate_max,
           c.n_distinct_usable > 1 AS has_conflict, t.support AS selected_support, t.n_tied,
           concat_ws('+',
               CASE WHEN c.n_types > 1 THEN 'rate_type_preference' END,
               CASE WHEN c.has_non_outlier AND o.n_outlier_rows > 0 THEN 'outliers_set_aside' END,
               CASE WHEN t.n_candidates = 1 THEN 'single_rate' WHEN t.n_tied = 1 THEN 'most_frequent_rate' ELSE 'lower_median_of_tied_rates' END
           ) AS selection_rule,
           t.provider_group_ids, t.network_hint, c.network_label, t.group_median
    FROM tied t JOIN ctx c USING ({key}) JOIN ctx_outliers o USING ({key})
    WHERE t.pos = CAST(ceil(t.n_tied / 2.0) AS INTEGER);
    """


ORDER = "npi, tin, billing_code_type, billing_code, associated_code, service_key, billing_class, setting, service_code, billing_code_modifier, negotiation_arrangement"


def select_rates(cfg, file_dir, as_of):
    c3, c4 = cfg["c3"], cfg.get("c4") or {}
    con = connect_rates(file_dir, cfg)
    con.execute(selection_sql(c3, c4, as_of))
    drops = dict(con.execute("SELECT drop_reason, count(*) FROM flagged WHERE drop_reason IS NOT NULL GROUP BY 1 ORDER BY 1").fetchall())
    flagged_rows = con.execute("SELECT count(*) FROM flagged").fetchone()[0]
    out = file_dir / "rates_selected.parquet"
    con.execute(f"COPY (SELECT {sql_str(file_dir.name)} AS file_key, * FROM selected ORDER BY {ORDER} NULLS FIRST) "
                f"TO {quote_path(out)} (FORMAT parquet, COMPRESSION zstd)")
    excluded = con.execute("""SELECT network_label, npi, tin, string_agg(DISTINCT provider_group_id, '|' ORDER BY provider_group_id) AS provider_group_ids,
                                     min(business_name) AS business_name, count(*) AS rows
                              FROM flagged WHERE drop_reason = 'non_ppo_evidence' GROUP BY ALL ORDER BY network_label, npi, tin""").df()
    excluded.to_csv(file_dir / "non_ppo_exclusions.csv", index=False)
    summary = con.execute("""SELECT selection_rule, count(*) AS contexts, sum(n_candidate_rows) AS candidate_rows FROM selected
                             GROUP BY 1 ORDER BY 2 DESC, 1""").df()
    stats = con.execute("""SELECT count(*) AS selected_rows, count(DISTINCT npi) AS npis, count(DISTINCT tin) AS tins,
                                  count(DISTINCT (npi, tin)) AS npi_tin_pairs, count(DISTINCT procedure_code) AS procedure_codes,
                                  count(DISTINCT billing_code_type || ':' || billing_code) AS billing_codes,
                                  count(*) FILTER (WHERE has_conflict) AS contexts_with_conflict,
                                  count(*) FILTER (WHERE procedure_code IS NOT NULL) AS rows_with_procedure_code,
                                  count(*) FILTER (WHERE associated_code IS NOT NULL) AS rows_with_associated_code,
                                  count(*) FILTER (WHERE n_service_names > 1) AS rows_collapsed_across_names,
                                  count(*) FILTER (WHERE network_hint IS NOT NULL) AS rows_with_network_hint
                           FROM selected""").df().iloc[0].to_dict()
    labels = dict(con.execute("SELECT network_label, count(*) FROM selected GROUP BY 1 ORDER BY 1").fetchall())
    excl = con.execute("""SELECT count(DISTINCT npi), count(DISTINCT tin), count(DISTINCT provider_group_id) FROM flagged
                          WHERE drop_reason = 'non_ppo_evidence'""").fetchone()
    examples = con.execute(f"""SELECT npi, tin, billing_code_type, billing_code, associated_code, name, billing_class, negotiated_rate,
                                     rate_min, rate_max, n_candidate_rows, selection_rule FROM selected WHERE has_conflict
                              ORDER BY rate_max / rate_min DESC, {ORDER} LIMIT 10""").df()
    con.close()
    summary.to_csv(file_dir / "selection_summary.csv", index=False)
    return {"pairs_before_selection": flagged_rows, "dropped_rows": drops,
            **{k: int(v) for k, v in stats.items()}, "selected_rows_by_network_label": {k: int(v) for k, v in labels.items()},
            "non_ppo_excluded": {"npis": excl[0], "tins": excl[1], "provider_groups": excl[2]},
            "by_rule": summary.to_dict("records"), "conflict_examples": examples.to_dict("records")}


def needs_extract(cfg, file_dir):
    if not (cfg.get("c4") or {}).get("reuse_extract", True):
        return True
    hdr = file_dir / "file_header.json"
    if not ((file_dir / "rates.parquet").exists() and hdr.exists()):
        return True
    return ptd_reader.load_header(file_dir).get("extract_version") != EXTRACT_VERSION


def extract_one(args):
    file_key, url, ptd_dir, out_root, parallel = args
    header = ptd_reader.extract_file(file_key, url, ptd_dir, out_root, parallel)
    header["extract_version"] = EXTRACT_VERSION
    write_json(header, Path(out_root) / file_key / "file_header.json")
    return header


def run(cfg):
    ppo = cfg["output_dir"] / "c2" / "ppo_files.csv"
    if not ppo.exists():
        log.warning("c4: %s missing; run --stage c2 first", ppo)
        return {}
    files = pd.read_csv(ppo, keep_default_na=False)
    out_root = cfg["output_dir"] / "c4"
    headers, todo = {}, []
    for f in files.itertuples(index=False):
        file_dir = out_root / f.file_key
        if needs_extract(cfg, file_dir):
            todo.append(f)
        else:
            log.info("c4: reusing extracted tables in %s", file_dir)
            headers[f.file_key] = ptd_reader.load_header(file_dir)
    workers = max(1, min(int(cfg.get("workers", 4)), len(todo)))
    jobs = [(f.file_key, f.file_url, cfg["ptd_dir"], out_root, workers == 1) for f in todo]
    if workers > 1:
        # one file per worker; inside a worker the two passes run one after the other
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for f, h in zip(todo, ex.map(extract_one, jobs)):
                headers[f.file_key] = h
    else:
        for f, job in zip(todo, jobs):
            headers[f.file_key] = extract_one(job)
    as_of = as_of_date(cfg, cfg["c3"], list(headers.values()))
    stats = {"as_of_date": as_of.isoformat(), "files": {}}
    for key in sorted(headers):
        s = select_rates(cfg, out_root / key, as_of)
        stats["files"][key] = {"extract": headers[key], "selection": s}
        log.info("c4: %s -> %s selected rates for %s NPIs / %s TINs; %s contexts had several rates; non-PPO evidence excluded %s NPIs",
                 key, f"{s['selected_rows']:,}", f"{s['npis']:,}", f"{s['tins']:,}", f"{s['contexts_with_conflict']:,}", s["non_ppo_excluded"]["npis"])
    write_json(stats, out_root / "c4_stats.json")
    return stats
