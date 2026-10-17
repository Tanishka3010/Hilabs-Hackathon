# Hilabs-Hackathon: Centene PTD pipeline

A reproducible pipeline over Centene's Transparency-in-Coverage (TiC) files. Scope: commercial **group**, **California**, **PPO** network.

| stage | what it does | outputs |
|---|---|---|
| `recon` | Streams every index file and profiles its schema, plans and file URLs | `outputs/c1/recon_report.md` |
| `c1` | Parses all index files into plan × file, deduplicated file and plan tables | `outputs/c1/*.parquet`, `*.csv`, `c1_stats.json` |
| `c2` | Applies the scope filters and the network-type rule cascade to every plan and file | `outputs/c2/plan_classification.csv`, `file_classification.csv`, `ppo_plans.csv`, `ppo_files.csv`, `exclusions_summary.csv`, `conflicts.csv`, `c2_stats.json` |
| `c4` | Downloads each PPO rate file to `raw/ptd/` (resumable) and streams it into rate tables (rate selection still to come) | `outputs/c4/<file_key>/rates.parquet`, `provider_refs.parquet`, `file_header.json` |
| `c3` | Grades every file used: 13 quality metrics per rate file, 5 per index file, with a weighted score and grade | `outputs/c3/file_scores.csv`, `metric_values.csv`, `metric_details.json`, `index_file_scores.csv`, `c3_stats.json` |
| `c5` | Provider location (in progress) | `outputs/c5` |

The logic and the reasons behind each decision are in [docs/logic_c1_c2.md](docs/logic_c1_c2.md) (C1, C2) and [docs/logic_c3.md](docs/logic_c3.md) (C3 file-quality metrics). Unresolved data ambiguities are in [docs/open_questions.md](docs/open_questions.md).

## Setup (Python 3.11+)
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Expected data layout
All paths are set in `config/config.yaml`, relative to the repo root, and each one can be overridden on the command line. Per-stage settings live in `config/stages/*.yaml`, which are merged automatically.
```
DATA/                 index_dir      the 11 Centene TiC index JSON files (read-only, committed)
raw/nppes/            nppes_dir      NPPES npidata_pfile_*.csv and pl_pfile_*.csv (git-ignored)
raw/ptd/              ptd_dir        full in-network rate files, downloaded and cached (git-ignored)
reference/            reference_dir  NUCC taxonomy, HiLabs benchmark xlsx, CMS DAC
outputs/              output_dir     generated; safe to delete and regenerate
```
Large inputs go in `raw/`, not `data/`: on macOS `data/` and `DATA/` are the same folder.

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
```bash
.venv/bin/python -m src.run --stage c2
```
Add the optional provider fingerprint (streams up to 40 MB of each ambiguous in-network file; results are cached in `outputs/c2/fingerprint_cache/`):
```bash
.venv/bin/python -m src.run --stage c2 --fingerprint
```
Extract the PPO rate file and grade the files (`c4` downloads about 700 MB the first time; NPPES-based metrics need `raw/nppes/npidata_pfile_*.csv`):
```bash
.venv/bin/python -m src.run --stage c4
```
```bash
.venv/bin/python -m src.run --stage c3
```

Useful options:
- `--index-dir`, `--output-dir`, `--nppes-dir`, `--reference-dir` override the config paths.
- `--workers N` sets the number of parallel processes.
- `--probe-mb 0` turns off the recon probe. By default the probe streams the first 40 MB of the CA in-network file to check for network markers, so it needs internet access; if the file can't be reached, the report says so.

Running `c2` or `all` ends with a short summary (index files, unique plans, files before and after dedup, in-scope plans, PPO plans and files, and the count for each exclusion reason).

Assumptions the organizers haven't confirmed are switches in `config/config.yaml` → `c2` (`eoa_as`, `ppo_epo_as`, `hsp_as`), so changing one never needs a code change. The organizers confirmed `eoa_as: eoa` and `ppo_epo_as: ppo`. Re-running overwrites every output deterministically: rows are sorted and keys are stable hashes.

## Current result (snapshot 2026-09-28/30)
- 1,312 in-scope CA group plans, all Health Net.
- **456 PPO plans** (455 high confidence, 1 medium), including 3 PPO+EPO plans the organizers confirmed as PPO.
- Ruled out: 723 HMO, 127 EOA (a separate network, confirmed by the organizers) and 6 POS. No EPO-only plans exist.
- **1 PPO rate file**, `healthnet-ca_in-network.json`. It's labelled `MIXED` because every CA network shares it, so PPO rates must be filtered inside it (C3).

- C3: the CA rate file scores **0.887 (grade B)** on 11 of 13 metrics. The 2 NPPES-based metrics need the NPPES file. Its weak points are benchmark coverage on professional rates (68% of benchmark weight), procedure-code identifiability (75%) and outliers (1.6%). See [docs/logic_c3.md](docs/logic_c3.md).

## Tests
```bash
.venv/bin/python -m pytest -q
```
