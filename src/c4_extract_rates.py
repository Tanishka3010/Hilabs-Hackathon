import duckdb
import pandas as pd

from src import ptd_reader
from src.c3_quality import as_of_date, connect_rates, sql_list
from src.common import log, write_json

CONTEXT_COLS = ["npi", "tin", "negotiation_arrangement", "billing_code_type", "billing_code", "procedure_code", "name",
                "billing_code_modifier", "billing_class", "setting", "service_code"]
OUTLIER_GROUP = "billing_code_type, billing_code, coalesce(procedure_code, ''), coalesce(name, ''), billing_class, negotiated_type"
NETWORK_HINT = "upper(nullif(regexp_extract(business_name, '^\\s*([A-Za-z][A-Za-z /&-]{1,40}):', 1), ''))"


def ctx_key(prefix=""):
    return ", ".join(f"{prefix}{c}" for c in CONTEXT_COLS)


def selection_sql(c3, c4, as_of):
    placeholders = ", ".join(str(float(p)) for p in c3["placeholder_rates"])
    usable = sql_list([t.lower() for t in c3["usable_rate_types"]])
    pref = [t.lower() for t in c4.get("rate_type_preference") or c3["usable_rate_types"]]
    type_rank = "CASE lower(negotiated_type) " + " ".join(f"WHEN '{t}' THEN {i}" for i, t in enumerate(pref)) + f" ELSE {len(pref)} END"
    fold, min_group = float(c3["outlier_fold"]), int(c3["outlier_min_group"])
    open_ended = str(c3["open_ended_expiration"])
    key = ctx_key()
    return f"""
    CREATE TEMP TABLE flagged AS
    SELECT r.* REPLACE ({", ".join(f"coalesce(r.{c}, '') AS {c}" for c in CONTEXT_COLS if c not in ("npi", "tin"))}),
           refs.npi, coalesce(refs.tin, '') AS tin, refs.tin_type, {NETWORK_HINT} AS network_hint,
           CASE WHEN negotiated_rate IS NULL OR negotiated_rate <= 0 OR negotiated_rate IN ({placeholders}) THEN 'invalid_rate'
                WHEN coalesce(expiration_date, '') NOT IN ('', '{open_ended}') AND expiration_date < '{as_of.isoformat()}' THEN 'expired'
                WHEN lower(coalesce(negotiated_type, '')) NOT IN ({usable}) THEN 'non_dollar_rate_type'
           END AS drop_reason,
           {type_rank} AS type_rank,
           md5(concat_ws('|', {OUTLIER_GROUP})) AS grp
    FROM rates r JOIN refs USING (provider_group_id)
    WHERE refs.npi IS NOT NULL;

    CREATE TEMP TABLE medians AS
    SELECT grp, median(negotiated_rate) AS med FROM flagged WHERE negotiated_rate > 0 GROUP BY grp HAVING count(*) >= {min_group};

    CREATE TEMP TABLE usable AS
    SELECT f.*, coalesce(f.negotiated_rate > m.med * {fold} OR f.negotiated_rate < m.med / {fold}, false) AS is_outlier, m.med AS group_median
    FROM flagged f LEFT JOIN medians m USING (grp) WHERE f.drop_reason IS NULL;

    CREATE TEMP TABLE ctx AS
    SELECT {key}, min(type_rank) AS best_type_rank, count(*) AS n_usable_rows, count(DISTINCT negotiated_rate) AS n_distinct_usable,
           min(negotiated_rate) AS rate_min, max(negotiated_rate) AS rate_max,
           bool_or(NOT is_outlier) AS has_non_outlier, count(DISTINCT negotiated_type) AS n_types
    FROM usable GROUP BY ALL;

    CREATE TEMP TABLE ctx_outliers AS
    SELECT {key}, count(*) FILTER (WHERE u.is_outlier AND u.type_rank = c.best_type_rank) AS n_outlier_rows
    FROM usable u JOIN ctx c USING ({key}) GROUP BY ALL;

    CREATE TEMP TABLE cand AS
    SELECT u.* FROM usable u JOIN ctx c USING ({key})
    WHERE u.type_rank = c.best_type_rank AND (NOT u.is_outlier OR NOT c.has_non_outlier);

    CREATE TEMP TABLE votes AS
    SELECT {key}, negotiated_type, negotiated_rate, count(*) AS support, string_agg(DISTINCT provider_group_id, '|') AS provider_group_ids,
           any_value(tin_type) AS tin_type, any_value(network_hint) AS network_hint, any_value(group_median) AS group_median
    FROM cand GROUP BY ALL;

    CREATE TEMP TABLE ranked AS
    SELECT *, max(support) OVER w AS top_support, count(*) OVER w AS n_candidates,
           count(*) FILTER (WHERE support = top_support) OVER w AS n_tied
    FROM (SELECT *, max(support) OVER w AS top_support FROM votes WINDOW w AS (PARTITION BY {key})) v
    WINDOW w AS (PARTITION BY {key});

    CREATE TEMP TABLE tied AS
    SELECT *, row_number() OVER (PARTITION BY {key} ORDER BY negotiated_rate) AS pos FROM ranked WHERE support = top_support;

    CREATE TEMP TABLE selected AS
    SELECT t.npi, nullif(t.tin, '') AS tin, t.tin_type, nullif(t.negotiation_arrangement, '') AS negotiation_arrangement,
           t.billing_code_type, t.billing_code, nullif(t.procedure_code, '') AS procedure_code, nullif(t.name, '') AS name,
           nullif(t.billing_code_modifier, '') AS billing_code_modifier, nullif(t.billing_class, '') AS billing_class,
           nullif(t.setting, '') AS setting, nullif(t.service_code, '') AS service_code, t.negotiated_type, t.negotiated_rate,
           c.n_usable_rows AS n_candidate_rows, c.n_distinct_usable AS n_distinct_rates, c.rate_min, c.rate_max,
           c.n_distinct_usable > 1 AS has_conflict, t.support AS selected_support, t.n_tied,
           concat_ws('+',
               CASE WHEN c.n_types > 1 THEN 'rate_type_preference' END,
               CASE WHEN c.has_non_outlier AND o.n_outlier_rows > 0 THEN 'outliers_set_aside' END,
               CASE WHEN t.n_candidates = 1 THEN 'single_rate' WHEN t.n_tied = 1 THEN 'most_frequent_rate' ELSE 'lower_median_of_tied_rates' END
           ) AS selection_rule,
           t.provider_group_ids, t.network_hint, t.group_median
    FROM tied t JOIN ctx c USING ({key}) JOIN ctx_outliers o USING ({key})
    WHERE t.pos = CAST(ceil(t.n_tied / 2.0) AS INTEGER);
    """


def select_rates(cfg, file_dir, as_of):
    c3, c4 = cfg["c3"], cfg.get("c4") or {}
    con = connect_rates(file_dir)
    con.execute(selection_sql(c3, c4, as_of))
    drops = dict(con.execute("SELECT drop_reason, count(*) FROM flagged WHERE drop_reason IS NOT NULL GROUP BY 1").fetchall())
    flagged_rows = con.execute("SELECT count(*) FROM flagged").fetchone()[0]
    out = file_dir / "rates_selected.parquet"
    con.execute(f"COPY (SELECT '{file_dir.name}' AS file_key, * FROM selected ORDER BY npi, tin, billing_code_type, billing_code, procedure_code, name, "
                f"billing_class, setting, service_code, billing_code_modifier, negotiation_arrangement) TO '{out}' (FORMAT parquet, COMPRESSION zstd)")
    summary = con.execute("""SELECT selection_rule, count(*) AS contexts, sum(n_candidate_rows) AS candidate_rows FROM selected
                             GROUP BY 1 ORDER BY 2 DESC""").df()
    stats = con.execute("""SELECT count(*) AS selected_rows, count(DISTINCT npi) AS npis, count(DISTINCT tin) AS tins,
                                  count(DISTINCT (npi, tin)) AS npi_tin_pairs, count(DISTINCT procedure_code) AS procedure_codes,
                                  count(DISTINCT billing_code_type || ':' || billing_code) AS billing_codes,
                                  count(*) FILTER (WHERE has_conflict) AS contexts_with_conflict,
                                  count(*) FILTER (WHERE procedure_code IS NOT NULL) AS rows_with_procedure_code,
                                  count(*) FILTER (WHERE network_hint IS NOT NULL) AS rows_with_network_hint
                           FROM selected""").df().iloc[0].to_dict()
    examples = con.execute("""SELECT npi, tin, billing_code_type, billing_code, procedure_code, name, billing_class, negotiated_rate,
                                     rate_min, rate_max, n_candidate_rows, selection_rule FROM selected WHERE has_conflict
                              ORDER BY rate_max / rate_min DESC, npi LIMIT 10""").df()
    con.close()
    summary.to_csv(file_dir / "selection_summary.csv", index=False)
    return {"pairs_before_selection": flagged_rows, "dropped_rows": drops,
            **{k: int(v) for k, v in stats.items()}, "by_rule": summary.to_dict("records"),
            "conflict_examples": examples.to_dict("records")}


def run(cfg):
    ppo = cfg["output_dir"] / "c2" / "ppo_files.csv"
    if not ppo.exists():
        log.warning("c4: %s missing; run --stage c2 first", ppo)
        return {}
    files = pd.read_csv(ppo, keep_default_na=False)
    out_root = cfg["output_dir"] / "c4"
    headers = {}
    for f in files.itertuples(index=False):
        file_dir = out_root / f.file_key
        if (cfg.get("c4") or {}).get("reuse_extract", True) and (file_dir / "rates.parquet").exists() and (file_dir / "file_header.json").exists():
            log.info("c4: reusing extracted tables in %s", file_dir)
            headers[f.file_key] = ptd_reader.load_header(file_dir)
        else:
            headers[f.file_key] = ptd_reader.extract_file(f.file_key, f.file_url, cfg["ptd_dir"], out_root)
    as_of = as_of_date(cfg, cfg["c3"], list(headers.values()))
    stats = {"as_of_date": as_of.isoformat(), "files": {}}
    for key in headers:
        s = select_rates(cfg, out_root / key, as_of)
        stats["files"][key] = {"extract": headers[key], "selection": s}
        log.info("c4: %s -> %s selected rates for %s NPIs / %s TINs; %s contexts had several rates", key, f"{s['selected_rows']:,}",
                 f"{s['npis']:,}", f"{s['tins']:,}", f"{s['contexts_with_conflict']:,}")
    write_json(stats, out_root / "c4_stats.json")
    return stats
