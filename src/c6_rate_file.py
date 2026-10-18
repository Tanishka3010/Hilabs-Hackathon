import json
from datetime import date

import pandas as pd
import xlsxwriter

from src import reference_data as rd
from src.common import duck, log, sql_str, write_json

EXCEL_MAX_ROWS = 1_048_575

# The deliverable (brief, deliverable 1): one row per NPI x TIN x address x billing code, with exactly these columns.
EXCEL_COLUMNS = [("npi", "NPI", 12), ("tin", "TIN", 12), ("address", "Address", 60), ("billing_code", "Billing Code", 12),
                 ("specialty", "Specialty", 40), ("rate", "Rate", 12)]
TEXT_COLUMNS = {"tin", "billing_code"}
DELIVERABLE_KEY = ["npi", "tin", "address", "billing_code"]
# The detail file keeps every rate context; it is unique on this grain (checked on every run).
GRAIN = ["npi", "tin", "address_id", "billing_code_type", "billing_code", "associated_code", "service_key", "billing_class", "setting",
         "place_of_service", "billing_code_modifier", "negotiation_arrangement"]

COLUMN_NOTES = {
    "NPI": "National Provider Identifier from the PPO rate file (provider_references)",
    "TIN": "Tax ID the NPI bills under in the rate file",
    "Provider Name": "NPPES organisation name (Type 2) or first + last name (Type 1)",
    "Address": "Practice address for this TIN-NPI pair (C5): every verified or corroborated address, or the best candidate if there is none",
    "Address Status": "verified = two sources linked to the NPI by an identifier agree, or the CA licence register lists it; corroborated = NPPES agrees with a site found by name + ZIP; unverified = best candidate only; out_of_state / no_address / deactivated_npi as in C5",
    "Billing Code Type / Billing Code": "As published (CPT, HCPCS or RC = revenue code)",
    "Procedure Code": "CPT/HCPCS that identifies the service: the billing code itself, or (revenue-code rows) the code in additional_information when its name in the file matches the item",
    "Associated Code": "Revenue-code rows only: the CPT/HCPCS-shaped code Health Net publishes in additional_information. It usually is not the item's own service, so it is shown as published, not as the procedure",
    "Service Name": "Item name. When the file repeats the same rates under many item names, one row stands for all of them: 'N services: name | name ...'",
    "Billing Class / Setting / Place of Service / Modifier / Arrangement": "Rate context kept apart in C4 (professional vs institutional, inpatient/outpatient, CMS place-of-service code, modifier, ffs/bundle)",
    "Network Label": "In-file network evidence for the provider group: ppo_label (TIN business name says PPO), other_label, unlabelled. Rows with HMO-only labels or capitation are left out (C4)",
    "Specialty": "NUCC display name of the NPI's primary NPPES taxonomy; HiLabs specialty in the Parquet file where the crosswalk maps it",
    "Rate": "The one rate kept for this provider context by the C4 selection rule (same rate repeats across addresses of the same TIN-NPI pair)",
    "Rate Type": "negotiated / fee schedule / derived",
}


def pair_addresses(links, addr, prov):
    a = addr.rename(columns={"county_name": "county"})
    keep_cols = ["npi", "address_id", "address_line1", "address_line2", "units", "city", "state", "zip", "county", "latitude", "longitude",
                 "verified", "verification_strength", "verification_basis", "confidence", "sources", "in_target_state"]
    l = links.merge(a[[c for c in keep_cols if c in a.columns]], on=["npi", "address_id"], how="left")
    l["verified"] = l["verified"].fillna(False).astype(bool)
    pair_has_verified = l.groupby(["tin", "npi"])["verified"].transform("any")
    l = l[(pair_has_verified & l["verified"]) | (~pair_has_verified & (l["pair_rank"] == 1)) | l["address_id"].isna()].copy()
    status = prov.set_index("npi")["location_status"]
    strength = l["verification_strength"] if "verification_strength" in l else pd.Series("strict", index=l.index).where(l["verified"], "")
    l["address_status"] = [("verified" if sg != "corroborated" else "corroborated") if v else ("no_address" if pd.isna(aid) else
                           ("out_of_state" if not ins else ("deactivated_npi" if status.get(n) == "deactivated_npi" else "unverified")))
                           for v, sg, aid, ins, n in zip(l["verified"], strength.fillna(""), l["address_id"], l["in_target_state"].fillna(False), l["npi"])]
    parts = [l["address_line1"].fillna(""), l["address_line2"].fillna("")]
    l["address"] = [", ".join(x for x in (a1, a2) if x and str(x) != "nan") for a1, a2 in zip(*parts)]
    l["full_address"] = [", ".join(x for x in (a, c, f"{st if isinstance(st, str) else ''} {z if isinstance(z, str) else ''}".strip()) if isinstance(x, str) and x)
                         for a, c, st, z in zip(l["address"], l["city"], l["state"], l["zip"])]
    l["tin"] = l["tin"].fillna("")
    return l.drop(columns=["in_target_state"], errors="ignore")


def provider_attributes(cfg, npis):
    # load with the rate files' full NPI set (the same scope as C3 and C5, so the NPPES cache is shared), then keep the ones needed
    nppes = rd.load_nppes(cfg).drop_duplicates("npi")
    nppes = nppes[nppes["npi"].astype("int64").isin(npis)]
    people = (nppes["first_name"].fillna("") + " " + nppes["last_name"].fillna("")).str.strip()
    nppes["provider_name"] = nppes["org_name"].fillna("").where(nppes["entity_type"] == "2", people)
    nucc = rd.load_nucc(cfg).set_index("taxonomy_code")["display_name"]
    try:
        xw = rd.load_benchmark_taxonomy_xwalk(cfg).drop_duplicates("taxonomy_code").set_index("taxonomy_code")
    except FileNotFoundError:
        xw = pd.DataFrame(columns=["spec_cd", "spec_descr"])
    out = pd.DataFrame({"npi": nppes["npi"].astype("int64"), "provider_name": nppes["provider_name"], "entity_type": nppes["entity_type"],
                        "taxonomy_code": nppes["taxonomy_code"].fillna("")})
    out["specialty"] = out["taxonomy_code"].map(nucc).fillna("")
    out["hilabs_spec_cd"] = out["taxonomy_code"].map(xw["spec_cd"]).fillna("") if len(xw) else ""
    out["hilabs_specialty"] = out["taxonomy_code"].map(xw["spec_descr"]).fillna("") if len(xw) else ""
    return out


def build_long(con, out_path):
    con.execute(f"""
        COPY (
            SELECT r.npi, r.tin, r.tin_type, coalesce(p.provider_name, '') AS provider_name, coalesce(p.entity_type, '') AS entity_type,
                   a.address_id, coalesce(a.address, '') AS address, coalesce(a.full_address, '') AS full_address, a.address_line1, a.address_line2, a.units, a.city, a.state, a.zip, a.county,
                   a.latitude, a.longitude, coalesce(a.address_status, 'no_address') AS address_status, a.verification_basis,
                   a.confidence AS address_confidence, a.sources AS address_sources, a.link_evidence, a.pair_rank,
                   r.billing_code_type, r.billing_code, r.procedure_code, r.associated_code, r.service_key, r.name AS service_names,
                   r.n_service_names, r.description,
                   CASE WHEN r.n_service_names > 1 THEN r.n_service_names || ' services: ' || left(r.name, 160) || CASE WHEN length(r.name) > 160 THEN ' ...' ELSE '' END
                        ELSE r.name END AS service_label,
                   r.billing_code_modifier, r.billing_class, r.setting, r.service_code AS place_of_service, r.negotiation_arrangement,
                   r.negotiated_type AS rate_type, r.negotiated_rate AS rate, r.rate_min, r.rate_max, r.n_candidate_rows, r.n_distinct_rates, r.selection_rule,
                   coalesce(p.taxonomy_code, '') AS taxonomy_code, coalesce(p.specialty, '') AS specialty,
                   coalesce(p.hilabs_spec_cd, '') AS hilabs_spec_cd, coalesce(p.hilabs_specialty, '') AS hilabs_specialty,
                   r.network_hint, r.network_label, r.file_key, coalesce(v.npi_valid, false) AS npi_valid
            FROM rates r
            LEFT JOIN npi_check v ON v.npi = r.npi
            LEFT JOIN pairs a ON a.npi = r.npi AND a.tin = coalesce(r.tin, '')
            LEFT JOIN prov p ON p.npi = r.npi
            ORDER BY r.npi, r.tin, a.pair_rank NULLS LAST, a.address_id, r.billing_code_type, r.billing_code, r.associated_code NULLS FIRST,
                     r.service_key NULLS FIRST, r.billing_class, r.setting, r.service_code, r.billing_code_modifier NULLS FIRST, r.negotiation_arrangement
        ) TO {sql_str(out_path)} (FORMAT parquet, COMPRESSION zstd)""")


def build_deliverable(con, detail_q, out_parquet, out_csv):
    """Collapses the detail rows to the brief's grain: one row per NPI x TIN x address x billing code.
    A billing code can carry several rates for one provider (different associated code, place of service, modifier, setting or
    arrangement). One is kept by a fixed rule:
      1. prefer the base contract: fee-for-service, no associated code, no modifier;
      2. among the rates left, take the lower median (position ceil(n/2) of the sorted distinct rates), as C4 does for ties.
    Every rate context stays in provider_rate_detail.parquet."""
    con.execute(f"""
        COPY (
            WITH d AS (
                SELECT npi, coalesce(tin, '') AS tin, full_address AS address, billing_code, specialty, rate,
                       (CASE WHEN coalesce(negotiation_arrangement, 'ffs') = 'ffs' THEN 0 ELSE 1 END) * 4
                     + (CASE WHEN associated_code IS NULL THEN 0 ELSE 1 END) * 2
                     + (CASE WHEN billing_code_modifier IS NULL THEN 0 ELSE 1 END) AS pref
                FROM {detail_q} WHERE npi_valid),
            best AS (SELECT * FROM d QUALIFY pref = min(pref) OVER (PARTITION BY npi, tin, address, billing_code)),
            rates AS (SELECT npi, tin, address, billing_code, any_value(specialty) AS specialty, list_sort(list(DISTINCT rate)) AS rs
                      FROM best GROUP BY npi, tin, address, billing_code)
            SELECT npi, tin, address, billing_code, specialty, rs[CAST(ceil(len(rs) / 2.0) AS INTEGER)] AS rate
            FROM rates ORDER BY npi, tin, address, billing_code
        ) TO {sql_str(out_parquet)} (FORMAT parquet, COMPRESSION zstd)""")
    con.execute(f"COPY (SELECT * FROM read_parquet({sql_str(out_parquet)})) TO {sql_str(out_csv)} (HEADER, DELIMITER ',')")
    q = f"read_parquet({sql_str(out_parquet)})"
    dup = con.execute(f"SELECT count(*) FROM (SELECT {', '.join(DELIVERABLE_KEY)} FROM {q} GROUP BY ALL HAVING count(*) > 1)").fetchone()[0]
    if dup:
        raise ValueError(f"c6: {dup} duplicate (NPI, TIN, address, billing code) keys in the rate file")
    rows, multi = con.execute(f"""SELECT (SELECT count(*) FROM {q}),
                                         (SELECT count(*) FROM (SELECT 1 FROM {detail_q} WHERE npi_valid
                                          GROUP BY npi, coalesce(tin, ''), full_address, billing_code HAVING count(DISTINCT rate) > 1))""").fetchone()
    return {"rows": int(rows), "duplicate_keys": 0, "keys_with_several_context_rates": int(multi)}


def grain_check(con, q):
    """Rows must be unique on the documented grain (NPI x TIN x address x billing code x the rate context shown in the Excel)."""
    key = ", ".join(f"coalesce(CAST({c} AS VARCHAR), '')" for c in GRAIN)
    dup = con.execute(f"SELECT count(*) FROM (SELECT {key} FROM {q} WHERE npi_valid GROUP BY ALL HAVING count(*) > 1)").fetchone()[0]
    base = con.execute(f"""SELECT count(*) FILTER (WHERE n > 1), coalesce(sum(n) FILTER (WHERE n > 1), 0) FROM
                           (SELECT count(*) AS n FROM {q} WHERE npi_valid GROUP BY npi, tin, address_id, billing_code_type, billing_code)""").fetchone()
    if dup:
        raise ValueError(f"c6: {dup} duplicate keys on the documented grain {GRAIN}")
    return {"duplicate_keys_on_full_grain": int(dup), "keys_npi_tin_address_code_with_several_rows": int(base[0]),
            "rows_sharing_npi_tin_address_code": int(base[1])}


def write_excel(con, parquet_path, xlsx_path, max_rows):
    """The rate file as Excel: only the six required columns, split into sheets under Excel's row limit."""
    wb = xlsxwriter.Workbook(str(xlsx_path), {"constant_memory": True, "strings_to_numbers": False, "strings_to_urls": False})
    bold = wb.add_format({"bold": True, "bg_color": "#DDEBF7", "border": 1})
    money = wb.add_format({"num_format": "#,##0.00"})
    text = wb.add_format({"num_format": "@"})
    cols = [c for c, _, _ in EXCEL_COLUMNS]
    i_npi = cols.index("npi")
    sheets, ws, row, last_npi = [], None, 0, None
    reader = con.execute(f"SELECT {', '.join(cols)} FROM read_parquet({sql_str(parquet_path)})").to_arrow_reader(200_000)
    for batch in reader:
        # column-wise conversion once per batch; None (missing) becomes an empty cell, numbers and text keep their types
        data = [batch.column(j).to_pylist() for j in range(len(cols))]
        for vals in zip(*data):
            npi = vals[i_npi]
            if ws is None or (row > max_rows and npi != last_npi) or row > EXCEL_MAX_ROWS:
                ws = wb.add_worksheet(f"Rates {len(sheets) + 1}")
                sheets.append(0)
                for j, (c, label, width) in enumerate(EXCEL_COLUMNS):
                    ws.set_column(j, j, width, money if c == "rate" else (text if c in TEXT_COLUMNS else None))
                    ws.write_string(0, j, label, bold)
                ws.freeze_panes(1, 0)
                row = 1
            ws.write_row(row, 0, ["" if v is None else (str(v) if cols[j] in TEXT_COLUMNS else v) for j, v in enumerate(vals)])
            row += 1
            sheets[-1] += 1
            last_npi = npi
    wb.close()
    return sheets


def load_stats(cfg, name):
    p = cfg["output_dir"] / name
    return json.loads(p.read_text()) if p.exists() else {}


def summary_metrics(cfg, long_stats):
    """Run summary (deliverable 4). Coverage is headlined per NPI and per TIN; the row-weighted share is shown separately because a few
    large hospital NPIs carry most rows."""
    c1, c2 = load_stats(cfg, "c1/c1_stats.json"), load_stats(cfg, "c2/c2_stats.json")
    c3, c4, c5 = load_stats(cfg, "c3/c3_stats.json"), load_stats(cfg, "c4/c4_stats.json"), load_stats(cfg, "c5/c5_stats.json")
    c7 = load_stats(cfg, "c7/c7_stats.json")
    files = c4.get("files") or {}
    sel = {k: sum(f.get("selection", {}).get(k, 0) or 0 for f in files.values()) for k in ("selected_rows", "contexts_with_conflict")}
    excl = {k: sum((f.get("selection", {}).get("non_ppo_excluded") or {}).get(k, 0) for f in files.values()) for k in ("npis", "provider_groups")}
    rate_scores = c3.get("rate_file_scores") or {}
    m = {
        "as_of_date": c3.get("as_of_date") or c4.get("as_of_date"),
        "share_npis_verified_address": c5.get("share_npis_verified"),
        "share_npis_verified_or_corroborated_address": c5.get("share_npis_verified_or_corroborated"),
        "share_tins_verified_address": c5.get("share_tins_verified"),
        "share_tins_verified_or_corroborated_address": c5.get("share_tins_verified_or_corroborated"),
        "share_tins_all_npis_verified": c5.get("share_tins_all_npis_verified"),
        "index_files_parsed": c1.get("index_files_parsed"),
        "unique_plans": c1.get("unique_plans"),
        "in_network_files_raw_to_unique": f"{c1.get('in_network_file_entries_raw')} -> {c1.get('unique_file_urls_normalized')}",
        "in_scope_ca_group_plans": c2.get("in_scope_plans"),
        "ppo_plans": c2.get("ppo_plans"),
        "ppo_rate_files": c2.get("ppo_files"),
        "rate_file_quality": "; ".join(f"{k}: {v.get('total')} ({v.get('grade')})" for k, v in rate_scores.items()),
        "index_file_quality": "; ".join(f"{k.split('_index')[0]}: {v.get('grade')}" for k, v in (c3.get("index_file_scores") or {}).items()),
        "prices_extracted": sum((f.get("extract") or {}).get("n_negotiated_prices") or 0 for f in files.values()),
        "npis_excluded_non_ppo_evidence": excl["npis"],
        "selected_rates_npi_tin_code_context": sel["selected_rows"],
        "contexts_with_several_rates": sel["contexts_with_conflict"],
        "unique_npis": c5.get("npis"),
        "unique_tins": c5.get("tins"),
        "npi_tin_pairs": c5.get("npi_tin_pairs"),
        "npis_with_verified_address": c5.get("npis_with_verified_address"),
        "npis_with_corroborated_address": c5.get("npis_with_corroborated_address"),
        "tins_with_verified_address": c5.get("tins_with_verified_address"),
        "tins_all_npis_verified": c5.get("tins_all_npis_verified"),
        "rate_file_rows_npi_tin_address_code": long_stats.get("rows"),
        "rate_file_npis": long_stats.get("npis"),
        "rate_file_npis_with_specialty": long_stats.get("npis_with_specialty"),
        "row_weighted_share_verified_or_corroborated": long_stats.get("share_rows_verified_or_corroborated"),
        "rate_rows_excluded_invalid_npi": long_stats.get("rows_excluded_invalid_npi"),
    }
    if c7:
        m.update({"c7_providers_scored": c7.get("providers_scored"), "c7_cost_effective_providers": (c7.get("by_label") or {}).get("cost_effective"),
                  "c7_high_cost_providers": (c7.get("by_label") or {}).get("high_cost")})
    return m


HEADLINE = ("share_npis_verified_address", "share_npis_verified_or_corroborated_address", "share_tins_verified_address",
            "share_tins_verified_or_corroborated_address", "share_tins_all_npis_verified")


def write_summary_md(path, metrics, long_stats, sheets):
    lines = ["# Run summary", "", f"Generated by `python -m src.run --stage all` on data as of {metrics.get('as_of_date')}.", "",
             "## Headline: provider linkage", "",
             "Shares are per NPI and per TIN. A TIN is linked to a verified address when at least one of its NPIs is; *all NPIs verified* is the stricter depth measure. *Verified* = two sources linked to the provider by an identifier (NPI or EIN) agree, or the CA licence register lists it. "
             "*Corroborated* = NPPES agrees with a site found by a name + ZIP search (weaker, because the search used the NPPES ZIP).", "",
             "| Metric | Value |", "|---|---|"]
    lines += [f"| {k.replace('_', ' ')} | {metrics.get(k)} |" for k in HEADLINE]
    lines += ["", "## All run metrics", "", "| Metric | Value |", "|---|---|"]
    lines += [f"| {k.replace('_', ' ')} | {v} |" for k, v in metrics.items() if k not in HEADLINE]
    lines += ["", "## Provider rate file", "",
              f"- `outputs/c6/provider_rate_file.xlsx` and `.csv`: {long_stats.get('rows', 0):,} rows, one per NPI x TIN x address x billing code, with the columns NPI, TIN, Address, Billing Code, Specialty, Rate (Excel: {len(sheets)} sheet(s)).",
              f"- Grain check: {long_stats.get('deliverable', {})}",
              f"- `outputs/c6/provider_rate_detail.parquet`: {long_stats.get('detail_rows', 0):,} rows with every rate context and evidence column (address status and sources, associated code, place of service, selection rule, ...).",
              "", "Detail rows by address status (row-weighted; large multi-site organisations dominate):", ""]
    lines += [f"- {k}: {v:,}" for k, v in (long_stats.get("rows_by_address_status") or {}).items()]
    path.write_text("\n".join(lines) + "\n")


def write_run_summary(cfg):
    """Rebuilds summary.json / SUMMARY.md from the stage stats (called by C6 and again by C7)."""
    c6s = load_stats(cfg, "c6/c6_stats.json")
    long_stats = {k: v for k, v in c6s.items() if k not in ("summary", "excel_sheets")}
    metrics = summary_metrics(cfg, long_stats)
    write_json(metrics, cfg["output_dir"] / "summary.json")
    write_summary_md(cfg["output_dir"] / "SUMMARY.md", metrics, long_stats, c6s.get("excel_sheets") or [])
    return metrics


def run(cfg):
    c6 = cfg.get("c6") or {}
    out = cfg["output_dir"] / "c6"
    out.mkdir(parents=True, exist_ok=True)
    rate_paths = sorted((cfg["output_dir"] / "c4").glob("*/rates_selected.parquet"))
    c5_dir = cfg["output_dir"] / "c5"
    if not rate_paths or not (c5_dir / "tin_npi_address.csv").exists():
        log.warning("c6: needs outputs/c4/*/rates_selected.parquet and outputs/c5/tin_npi_address.csv; run c4 and c5 first")
        return {}
    links = pd.read_csv(c5_dir / "tin_npi_address.csv", dtype={"tin": str})
    addr = pd.read_csv(c5_dir / "npi_addresses.csv", dtype={"zip": str, "county_fips": str})
    prov = pd.read_csv(c5_dir / "provider_locations.csv")
    pairs = pair_addresses(links, addr, prov)
    attrs = provider_attributes(cfg, set(prov["npi"].astype("int64")))
    con = duck(cfg)
    con.execute(f"CREATE VIEW rates AS SELECT * FROM read_parquet([{', '.join(sql_str(p) for p in rate_paths)}])")
    con.register("pairs", pairs)
    con.register("prov", attrs)
    rate_npis = con.execute("SELECT DISTINCT npi FROM rates").df()["npi"]
    con.register("npi_check", pd.DataFrame({"npi": rate_npis, "npi_valid": [rd.npi_is_valid(n) for n in rate_npis]}))
    detail_path = out / "provider_rate_detail.parquet"
    build_long(con, detail_path)
    q = f"read_parquet({sql_str(detail_path)})"
    grain = grain_check(con, q)
    rate_path, csv_path = out / "provider_rate_file.parquet", out / "provider_rate_file.csv"
    deliverable = build_deliverable(con, q, rate_path, csv_path)
    invalid_rows, invalid_tins = con.execute(f"SELECT count(*), count(DISTINCT tin) FROM {q} WHERE NOT npi_valid").fetchone()
    rows, verified, vc, npis, npis_spec = con.execute(f"""SELECT count(*), count(*) FILTER (WHERE address_status = 'verified'),
                                                      count(*) FILTER (WHERE address_status IN ('verified', 'corroborated')), count(DISTINCT npi),
                                                      count(DISTINCT npi) FILTER (WHERE specialty <> '') FROM {q} WHERE npi_valid""").fetchone()
    by_status = dict(con.execute(f"SELECT address_status, count(*) FROM {q} WHERE npi_valid GROUP BY 1 ORDER BY 2 DESC, 1").fetchall())
    long_stats = {"rows": deliverable["rows"], "detail_rows": int(rows), "rows_verified": int(verified),
                  "share_rows_verified": round(verified / rows, 4) if rows else None,
                  "share_rows_verified_or_corroborated": round(vc / rows, 4) if rows else None,
                  "npis": int(npis), "npis_with_specialty": int(npis_spec), "rows_by_address_status": {k: int(v) for k, v in by_status.items()},
                  "rows_excluded_invalid_npi": int(invalid_rows), "tins_with_invalid_npi_rows": int(invalid_tins), "grain": grain,
                  "deliverable": deliverable}
    xlsx_path = out / "provider_rate_file.xlsx"
    sheets = write_excel(con, rate_path, xlsx_path, int(c6.get("max_rows_per_sheet", 1_000_000)))
    con.close()
    stats = {**long_stats, "excel_sheets": sheets, "excel_bytes": xlsx_path.stat().st_size, "csv_bytes": csv_path.stat().st_size,
             "detail_parquet_bytes": detail_path.stat().st_size}
    write_json(stats, out / "c6_stats.json")
    stats["summary"] = write_run_summary(cfg)
    log.info("c6: rate file %s rows (NPI x TIN x address x billing code) in %d sheet(s), xlsx %.1f MB; detail %s rows", f"{deliverable['rows']:,}",
             len(sheets), xlsx_path.stat().st_size / 1e6, f"{rows:,}")
    return stats
