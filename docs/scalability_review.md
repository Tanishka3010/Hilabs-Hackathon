# Scalability review (against the "Scalability" criterion)

> Problem statement, Scalability (40 points): "Streaming instead of loading whole files, parallel processing, and a design that would extend beyond the in-scope data (Section 3)."

*Reviewed 2026-10-18, against `main` after C6. Use this as the to-do list when improving the pipeline.*

**Verdict: mostly met, not fully.**
- Streaming is solid everywhere.
- Parallelism is only partial.
- Several pieces are written specifically for California, one rate file, and uncompressed JSON. That would block extending beyond the in-scope data.

## 1. Where we stand

| Requirement | Status | Evidence |
|---|---|---|
| **Streaming** | ✅ Met | Index files and the 702 MB rate file are read with `ijson` (never `json.load`). Rates are written to Parquet in 250k-row batches (about 430 MB peak memory). NPPES (9.9M rows, 11 GB) and DAC are read in chunks. C3, C4 selection and C6 run in DuckDB, which works out of memory. The Excel file is written by `xlsxwriter` in constant-memory mode |
| **Parallel processing** | 🟡 Partial | Index files are parsed in a process pool (`--workers`), and the Census geocoder sends batches in parallel. But **rate files are extracted one after another** (`c4_extract_rates.run` loops), the two passes over each file (`provider_references`, then `in_network`) run in sequence, and **C3 grades files one at a time**. With one in-scope file this doesn't matter; with many it would |
| **Design that extends beyond scope** | 🟡 Partial | Good: all paths and thresholds are in config, C2's brand/state/market rules work for any payer, and C5 sources plug into one scoring rule (weights and families in `config/stages/c5.yaml`). Weak spots in §2 |

## 2. What would break or slow down outside our scope

1. **Compressed rate files.** Most payers publish `.json.gz`, often tens of GB. `src/ptd_reader.py` opens plain JSON only.
2. **Remote provider lists.** The TiC schema allows `provider_references` entries that point by URL to separate files. We only read lists embedded in the same file.
3. **Scope hard-coded in C2.** `TARGET_MARKET` (`group`) and `TARGET_NETWORK` (`PPO`) are constants in `src/c2_classify_network.py`. Only the state (`TARGET_STATE`, CA) is in config (`c3`/`c5` `target_state`), and only one state is allowed.
4. **California-only sources in C5.** The CDPH licensed-facility register, IRS `eo_ca.csv` and the county names (from the CA facility file) are tied to CA. Their loaders are separate functions in `src/reference_data.py` rather than entries in a list driven by config.
5. **C5 scoring loops in Python.** `score_addresses` loops over every NPI–address group, and the name + ZIP matchers use Python comprehensions over merged pairs. That's fine for 3.7k NPIs, but slow at national scale (millions of NPIs).
6. **Index files must be downloaded by hand into `DATA/`.** There's no fetching of the payer's index files from their published URLs.

## 3. How much change it needs

| # | Change | Effort | Benefit |
|---|---|---|---|
| 1 | Extract rate files in parallel (process pool, one file per worker), and run the two passes per file concurrently | Small (~1 hr) | True parallel processing for multi-file payers |
| 2 | Read `.json.gz` by streaming decompression (`gzip` wrapped round the stream) and fetch remote `provider_references` | Small–medium (~2 hrs) | Works with most other payers' files |
| 3 | Move market, network and states into a `config/stages/c2.yaml` and allow a **list** of target states (used by C2, C3, C5) | Small (~1 hr) | Re-scope to another state or network without code changes |
| 4 | Grade rate files in parallel in C3 (one DuckDB connection per file, in a process pool) | Small (~30 min) | Scales grading with the number of files |
| 5 | A C5 source registry in config (file pattern, join key: NPI / TIN / name + ZIP, weight, family, state, high-trust flag), with today's CA sources as entries | Medium (~2–3 hrs) | Add registries for other states (e.g. Texas, New York) by config alone |
| 6 | Move C5 scoring and name matching into DuckDB (set-based joins and aggregations instead of Python loops) | Medium (~3 hrs) | National-scale address mapping |
| 7 | **Proof run:** process 2–3 extra rate files (e.g. `healthnet-or`, 9 MB; `ambetter-ca`) through C2–C4 behind an `extra_files` option, and report timings and memory | Small (~1 hr) | Shows judges it actually scales, not just that it's designed to |
| 8 | (Optional) Download index files from the payer's index URLs into `DATA/`, using the resumable `ptd_reader.download` | Small (~1 hr) | Fully automated refresh from source |

## 4. Recommendation

**Do items 1, 2, 3, 4 and 7 first, about half a day in total.** Together they cover all three words in the criterion (streaming, parallel, extensible) with measured evidence.

Items 5 and 6 are the most work and matter only at national scale. If time is short, describe them in the logic docs as next steps rather than building them.

## 5. Related files
- Rate-file extraction: `src/ptd_reader.py`, `src/c4_extract_rates.py`
- Grading: `src/c3_quality.py`
- Network scope: `src/c2_classify_network.py`, `config/config.yaml` (`c2`)
- Address sources and scoring: `src/reference_data.py`, `src/c5_provider_location.py`, `config/stages/c5.yaml`
- Entry point and stage order: `src/run.py`
