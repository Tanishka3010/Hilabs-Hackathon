import pandas as pd

from src import ptd_reader
from src.common import log, write_json


def run(cfg):
    ppo = cfg["output_dir"] / "c2" / "ppo_files.csv"
    if not ppo.exists():
        log.warning("c4: %s missing; run --stage c2 first", ppo)
        return {}
    files = pd.read_csv(ppo, keep_default_na=False)
    out_root = cfg["output_dir"] / "c4"
    headers = {}
    for f in files.itertuples(index=False):
        headers[f.file_key] = ptd_reader.extract_file(f.file_key, f.file_url, cfg["ptd_dir"], out_root)
    write_json(headers, out_root / "c4_extract_stats.json")
    return headers
