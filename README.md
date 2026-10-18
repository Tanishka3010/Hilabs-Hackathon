# Hilabs-Hackathon: Centene PTD pipeline

A reproducible pipeline over Centene's Transparency-in-Coverage (TiC) files. Scope: commercial **group**, **California**, **PPO** network.

| stage | what it does | outputs |
|---|---|---|
| `recon` | Streams every index file and profiles its schema, plans and file URLs | `outputs/c1/recon_report.md` |
| `c1` | Parses all index files into plan × file, deduplicated file and plan tables | `outputs/c1/*.parquet`, `*.csv`, `c1_stats.json` |
| `c2` | Applies the scope filters and the network-type rule cascade to every plan and file | `outputs/c2/plan_classification.csv`, `file_classification.csv`, `ppo_plans.csv`, `ppo_files.csv`, `exclusions_summary.csv`, `conflicts.csv`, `c2_stats.json` |
| `c4` | Downloads each PPO rate file to `raw/ptd/` (resumable, retried; plain or gzip JSON), streams it into rate tables (files in parallel), collapses rates the file repeats under many service names, drops rows with in-file HMO evidence, then keeps one rate per provider context with a documented selection rule | `outputs/c4/<file_key>/rates_selected.parquet`, `rates.parquet`, `provider_refs.parquet`, `non_ppo_exclusions.csv`, `selection_summary.csv`, `c4_stats.json` |
| `c3` | Grades every file used: 14 quality metrics per rate file, 5 per index file, with a weighted score and grade (files graded in parallel) | `outputs/c3/file_scores.csv`, `metric_values.csv`, `metric_details.json`, `index_file_scores.csv`, `c3_stats.json` |
| `c5` | Maps every NPI and TIN to scored candidate addresses from 10 sources (CA licensed-facility register, CMS DAC, DAC group practices by name and by PAC ID via CMS PPEF, CMS POS CLIA sites, CA DCA licence boards, NPPES primary and secondary, IRS nonprofits), grades each as verified / corroborated / unverified, and links TIN → NPI → address | `outputs/c5/provider_locations.csv`, `npi_addresses.csv`, `tin_npi_address.csv`, `c5_stats.json` |
| `c6` | Builds the provider rate file: one row per NPI × TIN × address × billing code with columns NPI, TIN, Address, Billing Code, Specialty, Rate (Excel, CSV, Parquet), plus a detail file with every rate context; checks both grains and writes the run summary | `outputs/c6/provider_rate_file.xlsx`, `.csv`, `.parquet`, `provider_rate_detail.parquet`, `outputs/SUMMARY.md`, `outputs/summary.json` |
| `c7` | Cost effectiveness: compares each provider's rates with similar providers (same service, same specialty or facility type, same county) | `outputs/c7/provider_cost_effectiveness.csv`, `service_comparisons.parquet`, `c7_stats.json` |
| `ui` | One self-contained HTML page with an executive view and an analyst view | `outputs/ui/index.html` |

The logic and the reasons behind each decision are in [docs/logic_c1_c2.md](docs/logic_c1_c2.md) (C1, C2), [docs/logic_c3.md](docs/logic_c3.md) (C3 file-quality metrics), [docs/logic_c4.md](docs/logic_c4.md) (C4 rate extraction and selection), [docs/logic_c5.md](docs/logic_c5.md) (C5 provider addresses), [docs/logic_c6.md](docs/logic_c6.md) (C6 rate file) and [docs/logic_c7.md](docs/logic_c7.md) (C7 cost effectiveness). The run summary is [outputs/SUMMARY.md](outputs/SUMMARY.md). To resume the project in a new session, start with [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md). Unresolved data ambiguities are in [docs/open_questions.md](docs/open_questions.md).

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
raw/ptd/              ptd_dir        full in-network rate files (.json or .json.gz), downloaded and cached (git-ignored)
raw/sources/          c5.sources_dir health_facility_locations.csv (CDPH), eo_ca.csv (IRS exempt orgs, CA), POS_CLIA_*.csv (CMS), PPEF_Enrollment_Extract_*.csv (CMS), dca_*_Data00.tsv (CA DCA licensee lists) (git-ignored)
reference/            reference_dir  NUCC taxonomy, HiLabs benchmark xlsx, CMS DAC (DAC_NationalDownloadableFile.csv), directory_check_sample.csv
outputs/              output_dir     generated; safe to delete and regenerate (outputs/_cache holds reference, geocoder and DuckDB spill caches)
```
Large inputs go in `raw/`, not `data/`: on macOS `data/` and `DATA/` are the same folder.

Every C5 source is required: if one is missing, C5 stops and names it. To run without it on purpose, set `c5.allow_missing_sources: true` in `config/stages/c5.yaml`; the missing source is then listed in `outputs/c5/c5_stats.json` → `missing_sources`. `reference/directory_check_sample.csv` holds the manual Health Net directory spot check; C5 only creates it when it doesn't exist, and never overwrites it.

## Run
```bash
.venv/bin/python -m src.run --stage all
```
Individual stages, in pipeline order:
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
Map providers to addresses (the first run streams NPPES once and calls the US Census geocoder over the internet; both are cached):
```bash
.venv/bin/python -m src.run --stage c5
```
Build the rate file and the run summary, then the cost-effectiveness metrics and the UI:
```bash
.venv/bin/python -m src.run --stage c6
```
```bash
.venv/bin/python -m src.run --stage c7
```
```bash
.venv/bin/python -m src.run --stage ui
```
Open `outputs/ui/index.html` in a browser (no server needed).

Useful options:
- `--index-dir`, `--output-dir`, `--nppes-dir`, `--ptd-dir`, `--reference-dir` override the config paths.
- `--workers N` sets the number of parallel processes (index files, rate files and file grading run in parallel).
- `--probe-mb 0` turns off the recon probe. By default the probe streams the first 40 MB of the CA in-network file to check for network markers, so it needs internet access; if the file can't be reached, the report says so.

Running `c2` or `all` ends with a short summary (index files, unique plans, files before and after dedup, in-scope plans, PPO plans and files, and the count for each exclusion reason).

Configuration you may want to change (no code change needed):
- Scope: `config/config.yaml` → `c2.target_states` (a list), `c2.target_market`, `c2.target_network`.
- Assumptions the organizers haven't confirmed: `c2.eoa_as`, `c2.ppo_epo_as`, `c2.hsp_as`. The organizers confirmed `eoa_as: eoa` and `ppo_epo_as: ppo`.
- In-file PPO evidence: `config/stages/c4.yaml` → `exclude_non_ppo_evidence`, `non_ppo_arrangements`, label patterns.
- Memory: `config/config.yaml` → `duckdb.memory_limit` (default 2 GB); DuckDB spills to `outputs/_cache/duckdb` above it.

Re-running overwrites every output deterministically: rows are sorted on full keys and keys are stable hashes.

## Current result (snapshot 2026-09-28/30)
- **C2:** 1,312 in-scope CA group plans, all Health Net.
    - **456 PPO plans** (455 high confidence, 1 medium), including 3 PPO+EPO plans the organizers confirmed as PPO.
    - Ruled out: 723 HMO, 127 EOA (a separate network, confirmed by the organizers) and 6 POS. No EPO-only plans exist.
    - **1 PPO rate file**, `healthnet-ca_in-network.json`. It's labelled `MIXED` because every CA network shares it, so C4 carves PPO out of it using in-file evidence.
- **C3:** the CA rate file scores **0.765 (grade C)** on 14 metrics.
    - Its weak points: 69% of rows only repeat a rate set under another service name (`rate_replication`); 1.5% of distinct prices have a procedure code that identifies the service; specialty-matched coverage is 50% of benchmark weight; professional benchmark coverage is 68%.
    - 96.7% of its NPIs are valid, active and in CA. See [docs/logic_c3.md](docs/logic_c3.md).
- **C4:** **271,053 selected rates** for 3,716 NPIs and 2,873 TINs.
    - The 1.5 million published rows collapse to 458,759 real prices once copies across service names are removed.
    - Rows with in-file HMO evidence (capitation, HMO-only labels) are excluded: 91 NPIs in 34 provider groups.
    - 1,681 contexts had several rates; each kept one by a fixed rule. See [docs/logic_c4.md](docs/logic_c4.md).
- **C5:** 3,708 of 3,716 NPIs have a candidate address.
    - **1,610 NPIs (43.3%) have a verified address** (two identifier-linked sources agree, or the CA licence register).
    - **2,147 (57.8%) are verified or corroborated** (corroborated: NPPES plus a name + ZIP site).
    - TINs (through their NPIs): 49.2% verified, 57.8% verified or corroborated; 44.9% have every NPI verified.
    - The CMS PPEF (NPI → PAC ID → DAC group sites) and the CA DCA licence lists added 949 verified NPIs.
    - 93% of best addresses are confirmed by the US Census geocoder. See [docs/logic_c5.md](docs/logic_c5.md).
- **C6:** the rate file has **31,981 rows, one per NPI × TIN × address × billing code**, with exactly the columns NPI, TIN, Address, Billing Code, Specialty, Rate: `outputs/c6/provider_rate_file.xlsx`, `.csv` and `.parquet`. Every rate context and its evidence is in `provider_rate_detail.parquet` (474,967 rows). Every run checks both grains.
- **C7:** 685 providers have enough comparable services to be labelled: 192 cost effective, 334 in line, 159 high cost. See [docs/logic_c7.md](docs/logic_c7.md).
- **UI:** `outputs/ui/index.html`, with an executive view (funnel, file grade, linkage, cost mix) and an analyst view (searchable provider table with drill-down to services).

## Tests
```bash
.venv/bin/python -m pytest -q
```
