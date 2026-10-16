import argparse
import sys

from src import c0_recon, c1_parse_index
from src.common import load_config, log, setup_logging

STAGES = {"recon": [c0_recon], "c1": [c1_parse_index], "all": [c0_recon, c1_parse_index]}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m src.run")
    ap.add_argument("--stage", choices=sorted(STAGES) + ["c2"], required=True)
    ap.add_argument("--config")
    ap.add_argument("--index-dir")
    ap.add_argument("--nppes-dir")
    ap.add_argument("--reference-dir")
    ap.add_argument("--output-dir")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--probe-mb", type=float)
    a = ap.parse_args(argv)
    setup_logging()
    cfg = load_config(a.config, {"index_dir": a.index_dir, "nppes_dir": a.nppes_dir, "reference_dir": a.reference_dir,
                                 "output_dir": a.output_dir, "workers": a.workers})
    if a.probe_mb is not None:
        cfg.setdefault("recon", {})["probe_mb"] = a.probe_mb
    if a.stage == "c2":
        log.error("c2 is not implemented yet: its rule cascade is pending review of outputs/c1/recon_report.md")
        return 2
    for mod in STAGES[a.stage]:
        mod.run(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
