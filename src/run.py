import argparse
import json
import sys

from src import (c0_recon, c1_parse_index, c2_classify_network, c3_quality, c4_extract_rates, c5_provider_location, c6_rate_file,
                 c7_cost_effectiveness, ui_report)
from src.common import load_config, setup_logging

STAGES = {
    "recon": [c0_recon],
    "c1": [c1_parse_index],
    "c2": [c2_classify_network],
    "c4": [c4_extract_rates],
    "c3": [c3_quality],
    "c5": [c5_provider_location],
    "c6": [c6_rate_file],
    "c7": [c7_cost_effectiveness],
    "ui": [ui_report],
    "all": [c0_recon, c1_parse_index, c2_classify_network, c4_extract_rates, c3_quality, c5_provider_location, c6_rate_file,
            c7_cost_effectiveness, ui_report],
}


def print_summary(cfg):
    c1 = json.loads((cfg["output_dir"] / "c1" / "c1_stats.json").read_text())
    c2 = json.loads((cfg["output_dir"] / "c2" / "c2_stats.json").read_text())
    lines = [
        "",
        "=== Summary ===",
        f"index files parsed            {c1['index_files_parsed']}",
        f"unique plans                  {c1['unique_plans']}",
        f"in-network files (raw/dedup)  {c1['in_network_file_entries_raw']} / {c1['unique_file_urls_normalized']}",
        f"in-scope CA group plans       {c2['in_scope_plans']}",
        f"PPO plans                     {c2['ppo_plans']}  (by confidence: {c2['ppo_plans_by_confidence']}; upper bound if unresolved were PPO: {c2['ppo_upper_bound_if_unresolved_were_ppo']})",
        f"PPO files                     {c2['ppo_files']}  (labels: {c2['ppo_files_labels']})",
        "out-of-scope plans by reason  " + ", ".join(f"{k}={v}" for k, v in sorted(c2["out_of_scope_plans_by_primary_reason"].items())),
        "in-scope plans by decision    " + ", ".join(f"{k}={v}" for k, v in sorted(c2["in_scope_by_decision_reason"].items())),
        f"conflict rows                 {c2['conflicts_rows']} (in scope: {c2['conflicts_in_scope_rows']})",
    ]
    print("\n".join(lines))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m src.run")
    ap.add_argument("--stage", choices=sorted(STAGES), required=True)
    ap.add_argument("--config")
    ap.add_argument("--index-dir")
    ap.add_argument("--nppes-dir")
    ap.add_argument("--ptd-dir")
    ap.add_argument("--reference-dir")
    ap.add_argument("--output-dir")
    ap.add_argument("--sources-dir", help="folder with the C5 source files (default: c5.sources_dir in config/stages/c5.yaml)")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--probe-mb", type=float)
    ap.add_argument("--fingerprint", action="store_true")
    a = ap.parse_args(argv)
    setup_logging()
    cfg = load_config(a.config, {"index_dir": a.index_dir, "nppes_dir": a.nppes_dir, "ptd_dir": a.ptd_dir, "reference_dir": a.reference_dir,
                                 "output_dir": a.output_dir, "workers": a.workers})
    if a.sources_dir:
        cfg.setdefault("c5", {})["sources_dir"] = a.sources_dir
    if a.probe_mb is not None:
        cfg.setdefault("recon", {})["probe_mb"] = a.probe_mb
    stats = {}
    for mod in STAGES[a.stage]:
        stats[mod.__name__] = mod.run(cfg, fingerprint=a.fingerprint) if mod is c2_classify_network else mod.run(cfg)
    if a.stage in ("c2", "all"):
        print_summary(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
