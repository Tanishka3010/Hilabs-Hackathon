import json
import re
from datetime import date

from concurrent.futures import ProcessPoolExecutor

import pandas as pd

from src import reference_data as rd
from src.common import duck, log, sql_str, url_file_name, write_json
from src.contracts import CONTEXT_COLS, OUTLIER_GROUP_COLS, PROCEDURE_CODE_RE, PROCEDURE_CODE_TYPES, validate_header

METRICS = {
    "schema_validity": ("validity", "Share of price records that parse and carry every required field (code, code type, rate, rate type, billing class, expiration date)",
                        "A record that cannot be parsed or lacks a required field cannot be priced or compared at all"),
    "code_identifiability": ("validity", "Share of distinct prices tied to a standard procedure code: billing_code is CPT/HCPCS/DRG, or a revenue-code row whose additional_information code is the item's own service (its name matches that code's name in the file)",
                             "A revenue code alone (e.g. RC 636 'drugs') cannot be compared across payers; a code in additional_information only counts when it describes the item, because in this file it usually does not"),
    "referential_integrity": ("validity", "Share of rate rows whose provider reference resolves to an NPI",
                              "A rate that cannot be tied to a provider is unusable for provider-level benchmarking"),
    "code_coverage": ("completeness", "Share of benchmark codes (HiLabs Spec-Proc list) with at least one usable rate in the scored billing classes (default: professional), weighted by the codes' benchmark weight",
                      "A file is only useful for benchmarking if it prices the codes that drive spend; weighting stops a file scoring well on rare codes"),
    "specialty_code_coverage": ("completeness", "Share of benchmark (specialty, code) weight where a provider of that specialty (NPPES taxonomy -> HiLabs crosswalk) has a usable rate for that code",
                                "Contracting teams compare like with like: a cardiology code priced only for non-cardiologists does not support a cardiology benchmark"),
    "provider_validity": ("completeness", "Share of distinct NPIs that pass the check digit, exist in NPPES, are not deactivated and practise in the target state (NPPES primary or secondary address, or CMS DAC)",
                          "Invalid, retired or out-of-state providers inflate the network and point rates at people a CA contracting team cannot use"),
    "zero_invalid_rates": ("accuracy", "Share of rate rows that are null, <= $0 or a known placeholder value",
                           "$0 and placeholder rates are not real prices and drag averages down; tolerance 10% (score 0 at 10% bad)"),
    "outlier_rate": ("accuracy", "Share of positive rates more than outlier_fold x above or below the median of their (code type, code, procedure code, service name, billing class, rate type) group (groups with >= outlier_min_group rates)",
                     "Order-of-magnitude deviations usually mean unit errors (cents vs dollars, per-unit vs per-case) or misattributed codes; tolerance 5%"),
    "rate_type_usability": ("accuracy", "Share of rate rows whose negotiated_type converts directly to dollars (negotiated, fee schedule, derived)",
                            "Percentage-of-charges and per-diem rates cannot be compared as dollar amounts without a base that the file does not provide"),
    "rate_replication": ("uniqueness", "Share of price rows that only repeat, under another service name, the same rates a provider already has for that revenue code, associated code and place of service",
                         "When one rate set is copied across dozens of service names, the names do not price anything: the file looks granular but holds far fewer real prices; tolerance 50%"),
    "exact_duplicates": ("uniqueness", "Share of rate rows that repeat another row on every business field (provider, code, context, type, rate, expiration)",
                         "Exact duplicates inflate counts and storage but do not change prices, so they are tolerated up to 50%"),
    "conflicting_rates": ("uniqueness", "Share of provider contexts (the C4 context: NPI, TIN, arrangement, code, associated code, service, modifier, billing class, setting, place of service) that carry more than one distinct rate",
                          "Conflicting prices for the same service and provider make the 'true' rate ambiguous and force a selection rule; tolerance 10%"),
    "file_freshness": ("timeliness", "Days between the file's last_updated_on and the as-of date; full score up to 31 days, zero at 365",
                       "TiC requires monthly refreshes; an old file misstates today's contracts"),
    "expired_rates": ("timeliness", "Share of rate rows whose expiration_date is before the as-of date (9999-12-31 = open-ended)",
                      "Expired rates are no longer in force and must not be used as current prices; tolerance 10%"),
}

INDEX_METRICS = {
    "index_schema_completeness": "Share of plan entries with plan name, plan ID, ID type, market type and an in-network file URL",
    "index_id_consistency": "Share of plans whose plan_id_type agrees with the ID's shape and market (no C2 data flag)",
    "index_network_type_stated": "Share of plans whose name states a network type (PPO/HMO/EPO/POS/EOA)",
    "index_duplicate_entries": "1 - share of plan entries that repeat an entry already listed",
    "index_freshness": "last_updated_on of the index vs the as-of date (same scale as file_freshness)",
}

BUSINESS_COLS = ["provider_group_id", "negotiation_arrangement", "billing_code_type", "billing_code", "name", "billing_code_modifier", "billing_class",
                 "setting", "service_code", "negotiated_type", "negotiated_rate", "expiration_date", "additional_information"]
# A "cell" is one provider group's prices for one revenue/billing code in one context, ignoring the service name.
CELL_COLS = ["provider_group_id", "negotiation_arrangement", "billing_code_type", "billing_code", "additional_information", "billing_class",
             "setting", "service_code", "billing_code_modifier", "negotiated_type"]
OUTLIER_GROUP = ", ".join(f"coalesce({c}, '')" for c in OUTLIER_GROUP_COLS)


def share_score(good_share):
    return None if good_share is None else round(max(0.0, min(1.0, good_share)), 4)


def tolerance_score(bad_share, tolerance):
    return None if bad_share is None else round(max(0.0, 1.0 - bad_share / tolerance), 4)


def freshness_score(days, full, zero):
    if days is None:
        return None
    if days <= full:
        return 1.0
    return round(max(0.0, 1.0 - (days - full) / (zero - full)), 4)


def ratio(num, den):
    return None if not den else num / den


def grade(score, cuts):
    if score is None:
        return "N/A"
    for letter in ("A", "B", "C", "D"):
        if score >= cuts[letter]:
            return letter
    return "F"


def weighted_total(scores, weights):
    pairs = [(scores[k], weights.get(k, 1.0)) for k in scores if scores[k] is not None and weights.get(k, 1.0) > 0]
    den = sum(w for _, w in pairs)
    return round(sum(s * w for s, w in pairs) / den, 4) if den else None


def as_of_date(cfg, c3, headers=()):
    if c3.get("as_of_date"):
        return date.fromisoformat(str(c3["as_of_date"]))
    dates = [str(h.get("last_updated_on"))[:10] for h in headers if h.get("last_updated_on")]
    long = cfg["output_dir"] / "c1" / "plan_file_long.parquet"
    if long.exists():
        dates += [str(d)[:10] for d in pd.read_parquet(long, columns=["index_date"])["index_date"].dropna().unique()]
    valid = []
    for d in dates:
        try:
            valid.append(date.fromisoformat(d))
        except ValueError:
            pass
    return max(valid) if valid else date.today()


def days_between(later, earlier):
    try:
        return (later - date.fromisoformat(str(earlier)[:10])).days
    except (TypeError, ValueError):
        return None


def sql_list(values):
    return ", ".join(sql_str(v) for v in values)


def quote_path(path):
    return sql_str(path)


def connect_rates(file_dir, cfg=None):
    """Opens one extracted rate file as three relations:
    - rates_raw: every published price row (listing-level metrics: zero rates, rate types, expiry, exact duplicates);
    - rates: the same prices with name-replicated copies collapsed. Where a provider group carries the identical set of rates
      for one code/context under several item names, the names do not price anything, so one copy is kept, service_key is blanked
      and the names are kept in service_names. associated_code is a code-shaped additional_information on non-procedure codes;
      procedure_code is only set when it really identifies the service (CPT/HCPCS/DRG billing code, or an associated code whose
      name in the file matches the item name);
    - refs / r: provider groups and the NPI-level join."""
    con = duck(cfg)
    proc_types = sql_list(PROCEDURE_CODE_TYPES)
    cell_id = "md5(concat_ws('|', " + ", ".join(f"coalesce(CAST({c} AS VARCHAR), '~')" for c in CELL_COLS) + "))"
    con.execute(f"CREATE VIEW rates_raw AS SELECT *, {cell_id} AS cell_id FROM read_parquet({quote_path(file_dir / 'rates.parquet')})")
    con.execute(f"""CREATE TEMP TABLE code_names AS SELECT DISTINCT billing_code AS code, upper(trim(name)) AS nm FROM rates_raw
                    WHERE upper(billing_code_type) IN ({proc_types}) AND name IS NOT NULL""")
    con.execute("""CREATE TEMP TABLE cells AS
        WITH s AS (SELECT cell_id, coalesce(name, '') AS nm, list_sort(list(negotiated_rate)) AS rset FROM rates_raw GROUP BY ALL)
        SELECT cell_id, count(*) AS n_names, count(DISTINCT rset) AS n_rsets, min(nm) AS first_name,
               string_agg(nm, ' | ' ORDER BY nm) AS all_names FROM s GROUP BY cell_id""")
    con.execute(f"""CREATE TEMP TABLE rates AS
        SELECT r.*, c.n_names > 1 AND c.n_rsets = 1 AS name_replicated,
               CASE WHEN c.n_names > 1 AND c.n_rsets = 1 THEN '' ELSE coalesce(r.name, '') END AS service_key,
               CASE WHEN c.n_names > 1 AND c.n_rsets = 1 THEN c.all_names ELSE r.name END AS service_names,
               CASE WHEN c.n_names > 1 AND c.n_rsets = 1 THEN c.n_names ELSE 1 END AS n_service_names,
               CASE WHEN upper(r.billing_code_type) NOT IN ({proc_types}) AND regexp_matches(coalesce(r.additional_information, ''), '{PROCEDURE_CODE_RE}')
                    THEN r.additional_information END AS associated_code,
               CASE WHEN upper(r.billing_code_type) IN ({proc_types}) THEN r.billing_code
                    WHEN NOT (c.n_names > 1 AND c.n_rsets = 1) AND cn.code IS NOT NULL THEN r.additional_information END AS procedure_code
        FROM rates_raw r JOIN cells c USING (cell_id)
        LEFT JOIN code_names cn ON cn.code = r.additional_information AND cn.nm = upper(trim(r.name))
        WHERE NOT (c.n_names > 1 AND c.n_rsets = 1) OR coalesce(r.name, '') = c.first_name""")
    ref_files = [p for p in (file_dir / "provider_refs.parquet", file_dir / "provider_refs_inline.parquet") if p.exists()]
    con.execute(f"CREATE VIEW refs AS SELECT * FROM read_parquet([{', '.join(quote_path(p) for p in ref_files)}])")
    con.execute("""CREATE VIEW r AS SELECT rates.*, refs.npi, refs.tin_type, refs.tin, refs.business_name, refs.provider_group_id IS NOT NULL AS provider_ref_resolved
                   FROM rates LEFT JOIN refs USING (provider_group_id)""")
    return con


def usable_filter(c3, as_of):
    types = sql_list([t.lower() for t in c3["usable_rate_types"]])
    placeholders = ", ".join(str(float(p)) for p in c3["placeholder_rates"])
    return (f"negotiated_rate > 0 AND negotiated_rate NOT IN ({placeholders}) AND lower(negotiated_type) IN ({types}) "
            f"AND (expiration_date IS NULL OR expiration_date = '' OR expiration_date >= {sql_str(as_of.isoformat())})")


def rate_metrics(con, header, c3, as_of):
    """Listing-level metrics read every published row (rates_raw); metrics about distinct prices read the collapsed rates table."""
    tol = c3["tolerances"]
    placeholders = ", ".join(str(float(p)) for p in c3["placeholder_rates"])
    n = con.execute("SELECT count(*) FROM rates_raw").fetchone()[0]
    n_prices = con.execute("SELECT count(*) FROM rates").fetchone()[0]
    out, details = {}, {}

    invalid = sum(v for k, v in (header.get("invalid_records") or {}).items())
    records = (header.get("n_negotiated_prices") or 0) + invalid
    missing = con.execute("""SELECT count(*) FROM rates_raw WHERE coalesce(billing_code,'')='' OR coalesce(billing_code_type,'')='' OR negotiated_rate IS NULL
                             OR coalesce(negotiated_type,'')='' OR coalesce(billing_class,'')='' OR coalesce(expiration_date,'')=''""").fetchone()[0]
    parse_ok = 1 - ratio(invalid, records) if records else 1.0
    fields_ok = 1 - ratio(missing, n) if n else None
    out["schema_validity"] = (None if fields_ok is None else parse_ok * fields_ok,
                              share_score(None if fields_ok is None else parse_ok * fields_ok))
    details["schema_validity"] = {"invalid_records": header.get("invalid_records") or {}, "records": records, "rows_missing_required": missing, "rows": n}

    unresolved = con.execute("""SELECT count(*) FROM rates_raw WHERE provider_group_id IS NULL
                                OR provider_group_id NOT IN (SELECT DISTINCT provider_group_id FROM refs WHERE npi IS NOT NULL)""").fetchone()[0]
    v = 1 - ratio(unresolved, n) if n else None
    out["referential_integrity"] = (v, share_score(v))
    details["referential_integrity"] = {"rows_unresolved": unresolved, "rows": n}

    bad = con.execute(f"SELECT count(*) FROM rates_raw WHERE negotiated_rate IS NULL OR negotiated_rate <= 0 OR negotiated_rate IN ({placeholders})").fetchone()[0]
    v = ratio(bad, n)
    out["zero_invalid_rates"] = (v, tolerance_score(v, tol["zero_invalid_rates"]))
    details["zero_invalid_rates"] = dict(zip(["null", "zero_or_negative", "placeholder"], con.execute(
        f"""SELECT count(*) FILTER (WHERE negotiated_rate IS NULL), count(*) FILTER (WHERE negotiated_rate <= 0),
                   count(*) FILTER (WHERE negotiated_rate IN ({placeholders})) FROM rates_raw""").fetchone()))

    replicated = n - n_prices
    v = ratio(replicated, n)
    out["rate_replication"] = (v, tolerance_score(v, tol.get("rate_replication", 0.5)))
    rep = con.execute("""SELECT count(*) FILTER (WHERE n_names > 1 AND n_rsets = 1), count(*) FILTER (WHERE n_names > 1),
                                avg(n_names) FILTER (WHERE n_names > 1 AND n_rsets = 1) FROM cells""").fetchone()
    by_type = con.execute("""SELECT billing_code_type, count(*) AS raw_rows FROM rates_raw GROUP BY 1 ORDER BY 1""").df()
    kept = dict(con.execute("SELECT billing_code_type, count(*) FROM rates GROUP BY 1").fetchall())
    details["rate_replication"] = {"rows": n, "distinct_prices_after_collapse": n_prices, "rows_that_only_repeat_another_name": replicated,
                                   "cells_name_replicated": rep[0], "cells_with_several_names": rep[1],
                                   "avg_names_per_replicated_cell": round(rep[2] or 0, 2),
                                   "by_code_type": {t: {"rows": int(r), "distinct_prices": int(kept.get(t, 0))} for t, r in zip(by_type["billing_code_type"], by_type["raw_rows"])}}

    fold, min_group = float(c3["outlier_fold"]), int(c3["outlier_min_group"])
    con.execute(f"""CREATE TEMP TABLE scored AS
        WITH k AS (SELECT *, md5(concat_ws('|', {OUTLIER_GROUP})) AS grp FROM rates WHERE negotiated_rate > 0),
             g AS (SELECT grp, median(negotiated_rate) AS med FROM k GROUP BY grp HAVING count(*) >= {min_group})
        SELECT k.*, g.med, (k.negotiated_rate > g.med * {fold} OR k.negotiated_rate < g.med / {fold}) AS is_outlier FROM k JOIN g USING (grp)""")
    eligible, outliers = con.execute("SELECT count(*), count(*) FILTER (WHERE is_outlier) FROM scored").fetchone()
    v = ratio(outliers, eligible)
    out["outlier_rate"] = (v, tolerance_score(v, tol["outlier_rate"]))
    top = con.execute("""SELECT billing_code_type, billing_code, associated_code, service_key, billing_class, any_value(med) AS median,
                                count(*) AS outliers, min(negotiated_rate) AS min_rate, max(negotiated_rate) AS max_rate
                         FROM scored WHERE is_outlier GROUP BY ALL
                         ORDER BY outliers DESC, billing_code, associated_code NULLS FIRST, service_key, billing_class LIMIT 10""").df()
    details["outlier_rate"] = {"eligible_rows": eligible, "outlier_rows": outliers, "top_groups": top.to_dict("records")}

    identified, with_assoc = con.execute("SELECT count(*) FILTER (WHERE procedure_code IS NOT NULL), count(*) FILTER (WHERE associated_code IS NOT NULL) FROM rates").fetchone()
    v = ratio(identified, n_prices)
    out["code_identifiability"] = (v, share_score(v))
    details["code_identifiability"] = {"distinct_prices": n_prices, "prices_with_procedure_code": identified,
                                       "prices_with_associated_code_only": with_assoc - con.execute(
                                           "SELECT count(*) FROM rates WHERE associated_code IS NOT NULL AND procedure_code IS NOT NULL").fetchone()[0],
                                       "by_code_type": dict(con.execute(
        "SELECT billing_code_type || CASE WHEN procedure_code IS NULL THEN ' (no procedure code)' ELSE '' END, count(*) FROM rates GROUP BY 1 ORDER BY 1").fetchall())}

    types = sql_list([t.lower() for t in c3["usable_rate_types"]])
    mix = con.execute("SELECT lower(coalesce(negotiated_type,'(missing)')) AS t, count(*) AS n FROM rates_raw GROUP BY 1 ORDER BY 2 DESC, 1").df()
    usable = con.execute(f"SELECT count(*) FROM rates_raw WHERE lower(negotiated_type) IN ({types})").fetchone()[0]
    v = ratio(usable, n)
    out["rate_type_usability"] = (v, share_score(v))
    details["rate_type_usability"] = dict(zip(mix["t"], mix["n"].astype(int)))

    distinct = con.execute(f"SELECT count(*) FROM (SELECT DISTINCT {', '.join(BUSINESS_COLS)} FROM rates_raw)").fetchone()[0]
    v = 1 - ratio(distinct, n) if n else None
    out["exact_duplicates"] = (v, tolerance_score(v, tol["exact_duplicates"]))
    details["exact_duplicates"] = {"rows": n, "distinct_rows": distinct}

    key = ", ".join(CONTEXT_COLS)
    ctx, conflicting = con.execute(f"""SELECT count(*), count(*) FILTER (WHERE k > 1) FROM
        (SELECT count(DISTINCT negotiated_rate) AS k FROM r WHERE npi IS NOT NULL GROUP BY {key})""").fetchone()
    v = ratio(conflicting, ctx)
    out["conflicting_rates"] = (v, tolerance_score(v, tol["conflicting_rates"]))
    examples = con.execute(f"""SELECT {key}, count(DISTINCT negotiated_rate) AS n_rates, min(negotiated_rate) AS min_rate,
                                      max(negotiated_rate) AS max_rate FROM r WHERE npi IS NOT NULL GROUP BY {key}
                               HAVING count(DISTINCT negotiated_rate) > 1 ORDER BY max_rate / nullif(min_rate, 0) DESC NULLS LAST, {key} LIMIT 5""").df()
    details["conflicting_rates"] = {"contexts": ctx, "contexts_with_multiple_rates": conflicting, "widest_examples": examples.to_dict("records")}

    fd = c3["freshness_days"]
    days = days_between(as_of, header.get("last_updated_on"))
    out["file_freshness"] = (days, freshness_score(days, fd["full_score"], fd["zero_score"]))
    url_date = re.search(r"(\d{4}-\d{2}-\d{2})_", url_file_name(header.get("file_url") or ""))
    details["file_freshness"] = {"last_updated_on": header.get("last_updated_on"), "as_of": as_of.isoformat(), "days": days,
                                 "date_in_file_url": url_date.group(1) if url_date else None,
                                 "url_date_matches_content": (url_date.group(1) == str(header.get("last_updated_on"))[:10]) if url_date else None}

    open_ended = str(c3["open_ended_expiration"])
    expired = con.execute(f"""SELECT count(*) FROM rates_raw WHERE coalesce(expiration_date,'') NOT IN ('', {sql_str(open_ended)})
                              AND expiration_date < {sql_str(as_of.isoformat())}""").fetchone()[0]
    v = ratio(expired, n)
    out["expired_rates"] = (v, tolerance_score(v, tol["expired_rates"]))
    details["expired_rates"] = {"rows_expired": expired, "rows": n}
    details["network_labels"] = network_label_details(con)
    return out, details


def network_label_details(con):
    """Informational: how much of the file carries in-file network evidence (C2 cannot separate networks at file level)."""
    return {k: int(v) for k, v in con.execute(r"""SELECT
        count(DISTINCT provider_group_id) FILTER (WHERE regexp_matches(coalesce(business_name, ''), '(?i)\bPPO\b')) AS groups_ppo_label,
        count(DISTINCT provider_group_id) FILTER (WHERE regexp_matches(coalesce(business_name, ''), '(?i)\bHMO\b')
                                              AND NOT regexp_matches(coalesce(business_name, ''), '(?i)\bPPO\b')) AS groups_hmo_only_label,
        count(DISTINCT provider_group_id) AS groups FROM refs""").df().iloc[0].items()} | {
        "capitation_prices": int(con.execute("SELECT count(*) FROM rates_raw WHERE lower(coalesce(negotiation_arrangement, '')) = 'capitation'").fetchone()[0])}


def benchmark_weights(cfg):
    codes = rd.load_benchmark_codes(cfg)
    spec = rd.load_benchmark_specialty_weights(cfg)
    codes["spec_cd"] = codes["spec_cd"].astype(str)
    spec["spec_cd"] = spec["spec_cd"].astype(str)
    within = codes.groupby("spec_cd")["spec_proc_weight"].transform("sum")
    cells = codes.merge(spec[["spec_cd", "specialty_weight"]], on="spec_cd", how="left")
    cells["cell_weight"] = cells["specialty_weight"].fillna(0) * cells["spec_proc_weight"] / within
    code_w = cells.groupby("billing_code", as_index=False)["cell_weight"].sum().rename(columns={"cell_weight": "code_weight"})
    return cells[["spec_cd", "billing_code", "cell_weight"]], code_w


def coverage_metrics(con, cfg, c3, as_of):
    out, details = {}, {}
    cells, code_w = benchmark_weights(cfg)
    classes = sql_list([c.lower() for c in c3.get("coverage_billing_classes") or ["professional"]])
    priced_any = set(con.execute(f"SELECT DISTINCT procedure_code FROM rates WHERE procedure_code IS NOT NULL AND {usable_filter(c3, as_of)}").df()["procedure_code"].astype(str))
    priced = set(con.execute(f"""SELECT DISTINCT procedure_code FROM rates WHERE procedure_code IS NOT NULL AND lower(billing_class) IN ({classes})
                                 AND {usable_filter(c3, as_of)}""").df()["procedure_code"].astype(str))
    code_w["covered"] = code_w["billing_code"].isin(priced)
    v = ratio(code_w.loc[code_w["covered"], "code_weight"].sum(), code_w["code_weight"].sum())
    out["code_coverage"] = (v, share_score(v))
    missing = code_w[~code_w["covered"]].sort_values("code_weight", ascending=False)
    details["code_coverage"] = {"benchmark_codes": int(len(code_w)), "codes_priced": int(code_w["covered"].sum()),
                                "unweighted_share": round(code_w["covered"].mean(), 4) if len(code_w) else None,
                                "scored_billing_classes": c3.get("coverage_billing_classes") or ["professional"],
                                "codes_priced_any_class": int(code_w["billing_code"].isin(priced_any).sum()),
                                "weighted_share_any_class": round(ratio(code_w.loc[code_w["billing_code"].isin(priced_any), "code_weight"].sum(), code_w["code_weight"].sum()) or 0, 4),
                                "top_missing_codes": missing["billing_code"].head(20).tolist()}
    npis = set(int(x) for x in con.execute("SELECT DISTINCT npi FROM refs WHERE npi IS NOT NULL").df()["npi"])
    valid = {n for n in npis if rd.npi_is_valid(n)}
    details["provider_validity"] = {"distinct_npis": len(npis), "check_digit_valid": len(valid),
                                    "distinct_tins": con.execute("SELECT count(DISTINCT tin) FROM refs").fetchone()[0]}
    try:
        nppes = rd.load_nppes(cfg, npis=npis) if npis else pd.DataFrame(columns=["npi", "state", "nppes_status", "taxonomy_code"])
    except FileNotFoundError as e:
        log.warning("c3: NPPES unavailable (%s); provider_validity and specialty_code_coverage not scored", e)
        details["provider_validity"]["status"] = "unavailable: NPPES file missing"
        out["provider_validity"] = (None, None)
        out["specialty_code_coverage"] = (None, None)
        details["specialty_code_coverage"] = {"status": "unavailable: NPPES file missing"}
        return out, details
    target = c3["target_state"].upper()
    in_state = set(nppes.loc[nppes["state"] == target, "npi"].dropna().astype(int))
    for loader in (rd.load_pl, rd.load_dac):
        try:
            extra = loader(cfg, npis=npis)
            in_state |= set(extra.loc[extra["state"] == target, "npi"].dropna().astype(int))
        except FileNotFoundError as e:
            log.warning("c3: %s unavailable (%s)", loader.__name__, e)
    found = set(nppes["npi"].dropna().astype(int))
    active = set(nppes.loc[nppes["nppes_status"] != "deactivated", "npi"].dropna().astype(int))
    usable_npis = valid & found & active & in_state
    v = ratio(len(usable_npis), len(npis))
    out["provider_validity"] = (v, share_score(v))
    details["provider_validity"].update({"in_nppes": len(found & npis), "not_in_nppes": len(npis - found),
                                         "deactivated": len((found - active) & npis), "in_target_state": len(in_state & npis),
                                         "usable": len(usable_npis)})
    xwalk = rd.load_benchmark_taxonomy_xwalk(cfg)[["spec_cd", "taxonomy_code"]]
    npi_spec = nppes[["npi", "taxonomy_code"]].dropna().merge(xwalk, on="taxonomy_code")[["npi", "spec_cd"]].drop_duplicates()
    npi_spec["npi"] = npi_spec["npi"].astype("int64")
    con.register("npi_spec", npi_spec)
    covered_cells = con.execute(f"""SELECT DISTINCT n.spec_cd, rt.procedure_code AS billing_code FROM rates rt JOIN refs USING (provider_group_id)
                                    JOIN npi_spec n ON n.npi = refs.npi WHERE rt.procedure_code IS NOT NULL AND {usable_filter(c3, as_of)}""").df()
    cells = cells.merge(covered_cells.assign(covered=True), on=["spec_cd", "billing_code"], how="left")
    cells["covered"] = cells["covered"].fillna(False).astype(bool)
    v = ratio(cells.loc[cells["covered"], "cell_weight"].sum(), cells["cell_weight"].sum())
    out["specialty_code_coverage"] = (v, share_score(v))
    by_spec = cells.groupby("spec_cd").apply(lambda g: round(ratio(g.loc[g["covered"], "cell_weight"].sum(), g["cell_weight"].sum()) or 0, 4),
                                             include_groups=False)
    details["specialty_code_coverage"] = {"npis_with_benchmark_specialty": int(npi_spec["npi"].nunique()),
                                          "lowest_specialties": by_spec.sort_values().head(10).to_dict()}
    return out, details


def score_rate_file(cfg, c3, file_dir, as_of):
    header = validate_header(json.loads((file_dir / "file_header.json").read_text()))
    con = connect_rates(file_dir, cfg)
    values, details = rate_metrics(con, header, c3, as_of)
    cov_values, cov_details = coverage_metrics(con, cfg, c3, as_of)
    values.update(cov_values)
    details.update(cov_details)
    con.close()
    return header, values, details


def index_scorecard(cfg, c3, as_of):
    c1, c2 = cfg["output_dir"] / "c1", cfg["output_dir"] / "c2"
    if not (c1 / "plan_file_long.parquet").exists():
        return pd.DataFrame()
    long = pd.read_parquet(c1 / "plan_file_long.parquet")
    pc = pd.read_csv(c2 / "plan_classification.csv", keep_default_na=False) if (c2 / "plan_classification.csv").exists() else None
    fd = c3["freshness_days"]
    rows = []
    for name, g in long.groupby("source_index_file"):
        req = g[["plan_name", "plan_id", "plan_id_type", "plan_market_type", "file_url"]].fillna("").astype(str)
        complete = (req.apply(lambda c: c.str.strip() != "")).all(axis=1).mean()
        dups = g.duplicated(["plan_key", "file_key"]).mean()
        days = days_between(as_of, g["index_date"].iloc[0])
        scores = {"index_schema_completeness": share_score(complete), "index_duplicate_entries": share_score(1 - dups),
                  "index_freshness": freshness_score(days, fd["full_score"], fd["zero_score"])}
        raw = {"index_schema_completeness": complete, "index_duplicate_entries": dups, "index_freshness": days}
        if pc is not None:
            sub = pc[pc["plan_key"].isin(set(g["plan_key"]))]
            consistent = (sub["data_flags"] == "").mean() if len(sub) else None
            stated = (sub["name_label"] != "").mean() if len(sub) else None
            scores.update({"index_id_consistency": share_score(consistent), "index_network_type_stated": share_score(stated)})
            raw.update({"index_id_consistency": consistent, "index_network_type_stated": stated})
        total = weighted_total(scores, c3["index_weights"])
        rows.append({"index_file": name, "brand": g["brand"].iloc[0], "plan_entries": len(g), "total_score": total,
                     "grade": grade(total, c3["grades"]), **{f"score_{k}": v for k, v in scores.items()},
                     **{f"raw_{k}": (round(v, 4) if isinstance(v, float) else v) for k, v in raw.items()}})
    return pd.DataFrame(rows).sort_values(["brand", "index_file"]).reset_index(drop=True)


def file_names(cfg):
    p = cfg["output_dir"] / "c2" / "file_classification.csv"
    if not p.exists():
        return {}
    f = pd.read_csv(p, keep_default_na=False)
    return dict(zip(f["file_key"], f["file_name"]))


def round_value(v):
    return round(v, 6) if isinstance(v, float) else v


def run(cfg):
    c3 = cfg["c3"]
    out_dir = cfg["output_dir"] / "c3"
    out_dir.mkdir(parents=True, exist_ok=True)
    names = file_names(cfg)
    file_dirs = sorted(p.parent for p in (cfg["output_dir"] / "c4").glob("*/rates.parquet"))
    as_of = as_of_date(cfg, c3, [json.loads((d / "file_header.json").read_text()) for d in file_dirs])
    log.info("c3: as-of %s, %d rate file(s) to score", as_of, len(file_dirs))
    score_rows, value_rows, all_details = [], [], {}
    workers = max(1, min(int(cfg.get("workers", 4)), len(file_dirs)))
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            graded = list(ex.map(score_rate_file, [cfg] * len(file_dirs), [c3] * len(file_dirs), file_dirs, [as_of] * len(file_dirs)))
    else:
        graded = [score_rate_file(cfg, c3, d, as_of) for d in file_dirs]
    for d, (header, values, details) in zip(file_dirs, graded):
        scores = {k: v[1] for k, v in values.items()}
        total = weighted_total(scores, c3["weights"])
        key = header.get("file_key") or d.name
        score_rows.append({"file_key": key, "file_name": names.get(key, ""), "complete_file": header.get("complete"),
                           "rows": header.get("n_rows"), "total_score": total, "grade": grade(total, c3["grades"]),
                           "metrics_scored": sum(v is not None for v in scores.values()),
                           **{f"score_{k}": scores.get(k) for k in METRICS}})
        for k, (group, definition, why) in METRICS.items():
            raw, sc = values.get(k, (None, None))
            value_rows.append({"file_key": key, "metric": k, "group": group, "raw_value": round_value(raw), "score": sc,
                               "weight": c3["weights"].get(k, 1.0), "status": "scored" if sc is not None else "not scored",
                               "definition": definition, "why": why})
        all_details[key] = details
        log.info("c3: %s total=%s grade=%s", key, total, grade(total, c3["grades"]))
    if not file_dirs:
        log.warning("c3: no outputs/c4/*/rates.parquet yet; only index files are scored")
    scores_df = pd.DataFrame(score_rows, columns=["file_key", "file_name", "complete_file", "rows", "total_score", "grade", "metrics_scored",
                                                  *[f"score_{k}" for k in METRICS]])
    scores_df.to_csv(out_dir / "file_scores.csv", index=False)
    pd.DataFrame(value_rows, columns=["file_key", "metric", "group", "raw_value", "score", "weight", "status", "definition", "why"]).to_csv(
        out_dir / "metric_values.csv", index=False)
    write_json(all_details, out_dir / "metric_details.json")
    idx = index_scorecard(cfg, c3, as_of)
    idx.to_csv(out_dir / "index_file_scores.csv", index=False)
    stats = {"as_of_date": as_of.isoformat(), "rate_files_scored": len(score_rows),
             "rate_file_scores": {r["file_key"]: {"total": r["total_score"], "grade": r["grade"]} for r in score_rows},
             "index_files_scored": int(len(idx)),
             "index_file_scores": {r["index_file"]: {"total": r["total_score"], "grade": r["grade"]} for r in idx.to_dict("records")},
             "weights": c3["weights"], "index_weights": c3["index_weights"], "tolerances": c3["tolerances"],
             "metric_definitions": {k: {"group": g, "definition": d, "why": w} for k, (g, d, w) in METRICS.items()},
             "index_metric_definitions": INDEX_METRICS}
    write_json(stats, out_dir / "c3_stats.json")
    return stats
