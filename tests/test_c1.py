import json

import pandas as pd

from src import c1_parse_index
from tests.conftest import MIXED_URL, PPO_ONLY_URL


def run(cfg):
    stats = c1_parse_index.run(cfg)
    out = cfg["output_dir"] / "c1"
    return stats, pd.read_parquet(out / "plan_file_long.parquet"), pd.read_parquet(out / "files_dedup.parquet"), pd.read_parquet(out / "plans.parquet"), out


def test_c1_counts(cfg):
    stats, long, files, plans, _ = run(cfg)
    assert stats["index_files_parsed"] == 2
    assert stats["reporting_structure_entries"] == 6
    assert stats["plan_entries_raw"] == 7
    assert stats["plan_entries_without_file"] == 1
    assert len(long) == 8
    assert stats["unique_plans"] == 6


def test_c1_dedup_merges_query_case_port_whitespace(cfg):
    stats, long, files, _, _ = run(cfg)
    mixed = files[files["file_url"] == MIXED_URL.replace("EX.", "ex.")]
    assert len(mixed) == 1
    assert mixed.iloc[0]["n_plans"] == 3
    assert mixed.iloc[0]["n_raw_url_variants"] == 2
    assert stats["duplicate_urls_merged_by_normalization"] == 1


def test_c1_v1_variants_and_brand_from_filename(cfg):
    _, long, _, _, _ = run(cfg)
    v1 = long[long["source_index_file"].str.startswith("2026-01-01_ambetter")]
    assert set(v1["brand"]) == {"ambetter"} and set(v1["sub_index"]) == {"ca_sub"}
    assert v1["file_url"].isna().sum() == 1
    assert "ambetter-ca_in-network.json" in set(v1["file_name"].dropna())


def test_c1_reports_same_name_different_url(cfg):
    stats, _, _, _, out = run(cfg)
    coll = pd.read_csv(out / "same_name_diff_url.csv")
    assert stats["file_names_with_multiple_urls"] == 1
    assert set(coll["file_name"]) == {"2026-01-01_x_healthnet-ca_in-network.json"}
    assert coll["file_key"].nunique() == 2


def test_c1_plan_sharing_ppo_only_file(cfg):
    _, _, files, plans, _ = run(cfg)
    ppo = files[files["file_url"] == PPO_ONLY_URL]
    assert ppo.iloc[0]["n_plans"] == 1
    assert plans[plans["plan_name"] == "Gold PPO 500/20"].iloc[0]["n_files"] == 2


def test_c1_idempotent(cfg):
    run(cfg)
    out = cfg["output_dir"] / "c1"
    a = {p.name: p.read_bytes() for p in out.glob("*.csv")} | {"s": (out / "c1_stats.json").read_bytes()}
    run(cfg)
    b = {p.name: p.read_bytes() for p in out.glob("*.csv")} | {"s": (out / "c1_stats.json").read_bytes()}
    assert a == b
    assert json.loads(a["s"])["plan_file_rows"] == 8
