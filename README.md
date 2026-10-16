# Hilabs-Hackathon: Centene PTD pipeline

A reproducible pipeline over Centene's Transparency-in-Coverage (TiC) files. Scope: commercial **group**, **California**, **PPO** network.

| stage | what it does | outputs |
|---|---|---|
| `recon` | Streams every index file and profiles its schema, plans and file URLs | `outputs/c1/recon_report.md` |
| `c1` | Parses all index files into plan × file, deduplicated file and plan tables | `outputs/c1/*.parquet`, `*.csv`, `c1_stats.json` |
| `c2` | Classifies plans and files by network type (PPO/HMO/EPO/POS) | _in progress_ |

The logic and the reasons behind each decision are in [docs/logic_c1_c2.md](docs/logic_c1_c2.md). Unresolved data ambiguities are in [docs/open_questions.md](docs/open_questions.md).

## Setup (Python 3.11+)
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Expected data layout
All paths are set in `config/config.yaml`, relative to the repo root, and each one can be overridden on the command line.
```
DATA/                 index_dir      the 11 Centene TiC index JSON files (read-only)
data/nppes/           nppes_dir      NPPES monthly files (needed only for --fingerprint, later)
reference/            reference_dir  NUCC taxonomy, HiLabs benchmark xlsx, CMS DAC (not used by C1/C2)
outputs/              output_dir     generated; safe to delete and regenerate
```

## Run
```bash
.venv/bin/python -m src.run --stage all
```
Individual stages:
```bash
.venv/bin/python -m src.run --stage recon
```
```bash
.venv/bin/python -m src.run --stage c1
```

Useful options:
- `--index-dir`, `--output-dir`, `--nppes-dir`, `--reference-dir` override the config paths.
- `--workers N` sets the number of parallel processes.
- `--probe-mb 0` turns off the recon probe. By default the probe streams the first 40 MB of the CA in-network file to check for network markers, so it needs internet access; if the file can't be reached, the report says so.

Re-running overwrites every output deterministically: rows are sorted and keys are stable hashes.

## Tests
```bash
.venv/bin/python -m pytest -q
```
