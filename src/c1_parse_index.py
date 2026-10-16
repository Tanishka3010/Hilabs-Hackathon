import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

from src.common import (
    as_file_entries,
    iter_reporting_structure,
    join_sorted,
    list_index_files,
    load_config,
    log,
    normalize_url,
    parse_index_filename,
    read_header,
    setup_logging,
    stable_key,
    url_file_name,
    write_json,
    write_table,
)

LONG_COLS = [
    "source_index_file", "brand", "sub_index", "index_date", "index_version", "reporting_entity_name", "reporting_entity_type",
    "rs_index", "plan_name", "plan_id", "plan_id_type", "plan_market_type", "issuer_name", "plan_sponsor_name", "plan_key",
    "file_description", "file_url", "file_url_normalized", "file_name", "file_key", "allowed_amount_url",
]
PLAN_KEY_COLS = ["brand", "plan_name", "plan_id", "plan_id_type", "plan_market_type"]
SAMPLE_PLANS = 10


def clean(v):
    return v.strip() if isinstance(v, str) else v


def plan_key(brand, p):
    return stable_key("\x1f".join(str(clean(x) or "") for x in [brand, p.get("plan_name"), p.get("plan_id"), p.get("plan_id_type"), p.get("plan_market_type")]))


def parse_index_file(path):
    date, brand, sub = parse_index_filename(path.name)
    hdr = read_header(path)
    rows, n_rs, n_plan_entries, n_file_entries, n_plans_no_file = [], 0, 0, 0, 0
    for i, rs in enumerate(iter_reporting_structure(path)):
        n_rs += 1
        plans = rs.get("reporting_plans") or []
        files = as_file_entries(rs.get("in_network_files"))
        aaf = as_file_entries(rs.get("allowed_amount_file"))
        aaf_url = normalize_url(aaf[0]["location"]) if aaf else ""
        n_plan_entries += len(plans)
        n_file_entries += len(files)
        if not files:
            n_plans_no_file += len(plans)
            files = [{"description": None, "location": None}]
        for p in plans:
            base = {
                "source_index_file": path.name, "brand": brand, "sub_index": sub, "index_date": date or hdr.get("last_updated_on"),
                "index_version": hdr.get("version"), "reporting_entity_name": hdr.get("reporting_entity_name"),
                "reporting_entity_type": hdr.get("reporting_entity_type"), "rs_index": i,
                "plan_name": clean(p.get("plan_name")), "plan_id": clean(str(p["plan_id"])) if p.get("plan_id") is not None else None,
                "plan_id_type": clean(p.get("plan_id_type")), "plan_market_type": clean(p.get("plan_market_type")),
                "issuer_name": clean(p.get("issuer_name")), "plan_sponsor_name": clean(p.get("plan_sponsor_name")),
                "plan_key": plan_key(brand, p), "allowed_amount_url": aaf_url,
            }
            for f in files:
                raw = clean(f["location"])
                nu = normalize_url(raw) if raw else None
                rows.append({**base, "file_description": clean(f["description"]), "file_url": raw, "file_url_normalized": nu,
                             "file_name": url_file_name(nu) if nu else None, "file_key": stable_key(nu) if nu else None})
    return {"file": path.name, "brand": brand, "sub_index": sub, "rows": rows, "n_rs": n_rs, "n_plan_entries": n_plan_entries,
            "n_file_entries": n_file_entries, "n_plans_no_file": n_plans_no_file}


def build_files_dedup(long):
    f = long.dropna(subset=["file_key"])
    out = []
    for key, g in f.groupby("file_key", sort=True):
        plans = g.drop_duplicates("plan_key").sort_values(["plan_name", "plan_id"])
        out.append({
            "file_key": key,
            "file_url": sorted(g["file_url_normalized"].unique())[0],
            "file_name": g["file_name"].iloc[0],
            "file_description": join_sorted(g["file_description"]),
            "n_raw_url_variants": g["file_url"].nunique(),
            "raw_url_variants": join_sorted(g["file_url"]),
            "n_plan_entries": len(g),
            "n_plans": g["plan_key"].nunique(),
            "n_brands": g["brand"].nunique(),
            "brands": join_sorted(g["brand"]),
            "sub_indexes": join_sorted(g["sub_index"]),
            "market_types": join_sorted(g["plan_market_type"]),
            "plan_id_types": join_sorted(g["plan_id_type"]),
            "issuer_names": join_sorted(g["issuer_name"]),
            "sample_plan_names": " || ".join(plans["plan_name"].astype(str).head(SAMPLE_PLANS)),
            "index_files": join_sorted(g["source_index_file"]),
            "allowed_amount_urls": join_sorted(g["allowed_amount_url"]),
        })
    return pd.DataFrame(out).sort_values(["file_url", "file_key"], kind="stable").reset_index(drop=True)


def build_plans(long):
    out = []
    for key, g in long.groupby("plan_key", sort=False):
        r = g.iloc[0]
        out.append({
            "plan_key": key, **{c: r[c] for c in PLAN_KEY_COLS},
            "issuer_name": join_sorted(g["issuer_name"]), "plan_sponsor_name": join_sorted(g["plan_sponsor_name"]),
            "n_files": g["file_key"].nunique(), "file_keys": join_sorted(g["file_key"]), "file_names": join_sorted(g["file_name"]),
            "n_index_entries": g.drop_duplicates(["source_index_file", "rs_index"]).shape[0],
            "index_files": join_sorted(g["source_index_file"]), "sub_indexes": join_sorted(g["sub_index"]),
        })
    return pd.DataFrame(out).sort_values(["brand", "plan_name", "plan_id", "plan_key"], kind="stable").reset_index(drop=True)


def same_name_diff_url(files):
    g = files.groupby("file_name")["file_key"].nunique()
    names = sorted(g[g > 1].index)
    return files[files["file_name"].isin(names)][["file_name", "file_key", "file_url", "brands", "index_files"]].sort_values(["file_name", "file_url"])


def run(cfg):
    setup_logging()
    index_dir, out_dir = cfg["index_dir"], cfg["output_dir"] / "c1"
    paths = list_index_files(index_dir)
    log.info("c1: %d index files in %s", len(paths), index_dir)
    with ProcessPoolExecutor(max_workers=int(cfg.get("workers", 4))) as ex:
        parsed = list(ex.map(parse_index_file, paths))
    for p in parsed:
        log.info("c1 %-55s rs=%-3d plan_entries=%-5d file_entries=%-3d rows=%d", p["file"], p["n_rs"], p["n_plan_entries"], p["n_file_entries"], len(p["rows"]))
    long = pd.DataFrame([r for p in parsed for r in p["rows"]], columns=LONG_COLS)
    long = long.sort_values(["brand", "source_index_file", "rs_index", "plan_name", "plan_id", "file_url_normalized"], kind="stable", na_position="last").reset_index(drop=True)
    files = build_files_dedup(long)
    plans = build_plans(long)
    collisions = same_name_diff_url(files)
    write_table(long, out_dir, "plan_file_long")
    write_table(files, out_dir, "files_dedup")
    write_table(plans, out_dir, "plans")
    collisions.to_csv(out_dir / "same_name_diff_url.csv", index=False)
    n_raw = long["file_url"].dropna().nunique()
    n_norm = long["file_url_normalized"].dropna().nunique()
    per_brand = defaultdict(dict)
    for b, g in long.groupby("brand"):
        per_brand[b] = {
            "index_files": int(g["source_index_file"].nunique()),
            "plan_rows": int(len(g)),
            "unique_plans": int(g["plan_key"].nunique()),
            "unique_files": int(g["file_key"].nunique()),
            "market_types": {k: int(v) for k, v in plans[plans["brand"] == b]["plan_market_type"].value_counts().items()},
        }
    stats = {
        "index_files_parsed": len(parsed),
        "reporting_structure_entries": sum(p["n_rs"] for p in parsed),
        "plan_entries_raw": sum(p["n_plan_entries"] for p in parsed),
        "in_network_file_entries_raw": sum(p["n_file_entries"] for p in parsed),
        "plan_entries_without_file": sum(p["n_plans_no_file"] for p in parsed),
        "plan_file_rows": int(len(long)),
        "unique_plans": int(len(plans)),
        "unique_plans_in_multiple_index_files": int((plans["index_files"].str.count(r"\|") > 0).sum()),
        "unique_file_urls_raw": int(n_raw),
        "unique_file_urls_normalized": int(n_norm),
        "duplicate_urls_merged_by_normalization": int(n_raw - n_norm),
        "duplicate_file_entries_removed": int(sum(p["n_file_entries"] for p in parsed) - n_norm),
        "file_names_with_multiple_urls": int(collisions["file_name"].nunique()),
        "per_brand": dict(per_brand),
    }
    write_json(stats, out_dir / "c1_stats.json")
    log.info("c1: rows=%d unique_plans=%d urls raw=%d normalized=%d file_entries=%d -> %d unique files",
             len(long), len(plans), n_raw, n_norm, stats["in_network_file_entries_raw"], n_norm)
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--index-dir")
    ap.add_argument("--output-dir")
    ap.add_argument("--workers", type=int)
    a = ap.parse_args(argv)
    run(load_config(a.config, {"index_dir": a.index_dir, "output_dir": a.output_dir, "workers": a.workers}))


if __name__ == "__main__":
    main()
