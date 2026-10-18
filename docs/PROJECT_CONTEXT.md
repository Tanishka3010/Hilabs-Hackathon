# Project context: HiLabs PTD hackathon (Centene CA commercial-group PPO)

This is the single document to read before continuing the project in a new chat. It records what was built, every key number, every decision and its reason, where things live, and what's left.

The detailed logic is in four documents:
- [logic_c1_c2.md](logic_c1_c2.md) (C1, C2)
- [logic_c3.md](logic_c3.md) (C3)
- [logic_c4.md](logic_c4.md) (C4)
- [open_questions.md](open_questions.md)

*Last updated 2026-10-18. Branch `feat/location-c5` holds all the latest work (see §9).*

---

## 1. The problem (from `problem_statement.md`)

Build a pipeline over Centene's Transparency-in-Coverage (TiC) price-transparency data (PTD) that:
1. finds Centene's **PPO network** for the **commercial group market in California**;
2. **grades the quality** of the files used;
3. maps every provider (NPI, TIN) to a **verified practising address**;
4. produces a **provider-level rate file**: one row per NPI × TIN × address × billing code, with specialty and rate.

Provider types: Type 1 (individual) and Type 2 (organisation) NPIs.

| # | Challenge | Status |
|---|---|---|
| C1 | Parse the index files into one table (brand, plan name, plan ID, market type, rate-file URL), deduplicated | **Done** |
| C2 | Identify the PPO plans and rate files; explain how HMO, EPO and POS were ruled out | **Done**, organizer answers applied |
| C3 | Define PTD quality and completeness metrics, score every file used, justify each metric | **Done**: 13 rate-file metrics and 5 index-file metrics |
| C4 | Extract rates for every PPO provider; when a provider has several rates for one code, say which is kept and why | **Done** |
| C5 | Map TINs and NPIs to their practising address, without relying on NPPES alone | **Done**: 8 sources + Census geocoding; 1,899 NPIs (50.8%) and 1,435 TINs (49.7%) verified; 93% of best addresses geocoded; 41% of NPI–TIN pairs have TIN-specific evidence |
| C6 | Build the provider rate file (NPI, TIN, address, billing code, specialty, rate), as Excel | **Done**: 1,466,669 rows, 97% with a verified address; xlsx (103 MB, git-ignored, rebuilt by `--stage c6`) + Parquet (committed) |
| C7 (optional) | Cost-effectiveness metrics vs similar providers | Not started |

Deliverables:

| # | Deliverable | Status |
|---|---|---|
| 1 | Rate file (Excel) | Done: `outputs/c6/provider_rate_file.xlsx` (rebuilt locally) |
| 2 | Logic file | Network part done (`logic_c1_c2.md`); address part done (`logic_c5.md`) |
| 3 | File-quality metrics | Done (`logic_c3.md`) |
| 4 | Summary file | Done: `outputs/SUMMARY.md` + `summary.json` (written by C6) |
| 5 | Video (≤ 5 min) | Not started |
| 6 | Code repo | Re-runs from the README, no hard-coded paths |

Scoring weights (logic and metrics track): Logic 40, Metrics 35, Structure 25. Engineering track: Scalability 40, Coverage and linkage 35, Correctness and reproducibility 25.

## 2. Environment and how to run

- **Repo:** `https://github.com/Tanishka3010/Hilabs-Hackathon`. Work in any local clone; all paths are relative to the repo root (developed on macOS with Python 3.14 in `.venv`).
- **Setup:**
  ```bash
  python3 -m venv .venv
  ```
  ```bash
  .venv/bin/pip install -r requirements.txt
  ```
  `requirements.txt` pins ijson 3.6.0, pandas 3.0.6, pyarrow 26.0.0, PyYAML 6.0.3, pytest 9.1.1, openpyxl 3.1.5 and duckdb 1.5.6.
- **Single entry point:**
  ```bash
  .venv/bin/python -m src.run --stage all
  ```
  The stages are `recon`, `c1`, `c2`, `c4`, `c3` and `c5`. `all` runs them in that order; C4 runs before C3 because C3 grades the extracted tables.
  - Options: `--fingerprint` (C2 provider sample), `--probe-mb N` (recon), `--workers N`, and `--index-dir`, `--nppes-dir`, `--ptd-dir`, `--reference-dir`, `--output-dir`.
- **Tests:**
  ```bash
  .venv/bin/python -m pytest -q
  ```
  104 tests pass. They run without the large files: fixtures are generated, and real-file tests skip when a file is missing.
- **Config:** `config/config.yaml` holds the paths, relative to the repo root. `config/stages/c3.yaml`, `c4.yaml` and `c5.yaml` hold per-stage settings and are merged automatically by `load_config`. The `c2` settings live in `config.yaml`.

### Data layout
| Path | What | In git? |
|---|---|---|
| `DATA/` | The 11 Centene index files, snapshot 2026-09-28/30 | yes |
| `reference/` | `Rate Benchmarking Individual Weights_Hackathon.xlsx`, `nucc_taxonomy_261.csv`, `dac_ca.parquet` (CMS DAC, CA only, 314,486 rows) | yes |
| `raw/ptd/` | Downloaded full rate files (the CA file is 701,615,514 bytes, MD5 `dfe6872bf471b907e5a199858000a10f`) | no (`raw/` ignored) |
| `raw/nppes/` | `npidata_pfile_20050523-20261011.csv` (9.85M rows), `pl_pfile_20050523-20261011.csv` | no |
| `outputs/` | All generated outputs; `outputs/_cache/` is ignored | yes, except the cache |

**Gotcha:** the macOS filesystem ignores case, so `data/` and `DATA/` are the **same folder**. That's why large inputs live in `raw/`. Never git-ignore `data/`.

## 3. Repository map
| File | Role |
|---|---|
| `src/common.py` | Config loading (with stage configs), URL normalisation, stable hashes, streaming helpers, table writers |
| `src/c0_recon.py` | Step-0 recon of the index files → `outputs/c1/recon_report.md` |
| `src/c1_parse_index.py` | C1 parsing and dedup |
| `src/c2_classify_network.py`, `src/c2_fingerprint.py` | C2 rule cascade and the optional provider fingerprint |
| `src/ptd_reader.py` | Download and streaming extraction of an in-network file into Parquet |
| `src/c4_extract_rates.py` | C4 extraction stage plus the rate-selection rule |
| `src/c3_quality.py` | C3 metrics (DuckDB), scoring, index scorecard |
| `src/reference_data.py` | Loaders: benchmark xlsx (3 sheets), NUCC, DAC, NPPES, practice locations, and the `npi_is_valid` check digit |
| `src/contracts.py` | Column contracts for the rate tables |
| `src/c5_provider_location.py` | C5: address candidates, normalising, confidence scoring, verification, TIN–NPI–address links, county check, directory spot-check sample |
| `src/geocode.py` | Census batch geocoder client (multipart upload, parallel batches, cache, retries) |
| `src/c6_rate_file.py` | C6: rate file (Parquet + Excel split across sheets) and run summary |
| `src/run.py` | CLI entry point and summary print |
| `docs/` | `logic_c1_c2.md`, `logic_c3.md`, `logic_c4.md`, `open_questions.md`, `work_split.md` (the two-person split, **abandoned**: one person now does everything), this file, and a Word summary `HiLabs_PTD_C1_C2_Summary.docx` |

## 4. C1: index parsing (done)

**Logic.** Each index file is streamed with ijson, one `reporting_structure` item at a time, using a process pool. The brand comes from the file name; QualChoice sub-indexes roll up to `qualchoice` and keep `sub_index`. One row per plan × in-network file.

**Dedup.** Two entries are the same file only if their **normalized URLs** are equal. Normalizing trims spaces, lowercases the scheme and host, drops default ports, query strings and fragments, and canonicalizes percent-encoding, while keeping the path's case. `file_key` = sha1(normalized URL)[:16]. Files with the same name but a different URL are reported, not merged.

**Numbers.**
- 11 index files, all v2.0.0, all from "Centene Management Company LLC" (a TPA).
- 40 `reporting_structure` entries; 3,275 plan rows; 3,270 unique plans.
- 40 file entries reduced to **33 unique files** (7 QualChoice re-listings).
- 0 URLs merged only by normalization; 0 name collisions.

**Outputs:** `outputs/c1/plan_file_long` (the main table), `files_dedup`, `plans`, `same_name_diff_url`, `c1_stats.json`, `recon_report.md`.

## 5. C2: PPO network (done)

**Key fact.** Health Net CA has **one** in-network file for **all 1,312 CA group plans**: `2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json`. So the PPO network is identified **per plan**, and the file is **MIXED**.

**Scope filters (Stage A),** each exclusion with a reason code:
- State: from the URL token, then the HIOS plan ID state, then the brand's state (accepted only if ≥90% of its HIOS IDs agree).
- Market: `plan_market_type == group`.
- Product: non-commercial and dental/vision-only plans are guarded against (none matched).

| Outcome | Plans |
|---|---|
| Out of state | 1,900 (Ambetter outside CA 1,062, Health Net OR 381, QualChoice AR 363, Fidelis NY 94) |
| Individual market | 58 (Ambetter CA) |
| **In scope** | **1,312** |

**Network rules (Stage C precedence; no ML).** The first rule that fires decides:

| Rank | Rule |
|---|---|
| P1 | An explicit network word in the plan name (whole-word match; `POST` isn't POS) |
| P1b | A word glued to a known prefix (`CUSPPO`) |
| P1x | A special token mapped by config: EOA and PPO+EPO, both **confirmed by the organizers** |
| P2 | A word in the file name |
| P3 | A legal-entity rule |
| P4 | A product family name, accepted only if purity is 1.0 with at least 5 plans: SC, SAL, WholeCare, CalChoice, SmartCare, Full Network, Salud. `EC` is rejected because it appears on both HMO and EOA plans |
| P5 | File-sharing propagation |
| P6 | Cost-share shape (mostly rejected by its own validation) |

**Organizer answers (2026-10-17).** **EOA is a separate network, not PPO.** **Plans named both PPO and EPO count as PPO.** Config: `eoa_as: eoa`, `ppo_epo_as: ppo`, `organizer_confirmed: [eoa_as, ppo_epo_as]`.

**Result.**
- **456 PPO plans** (455 high confidence, 1 medium): 452 by an explicit word, 3 PPO+EPO, 1 `CUSPPO`.
- Ruled out: 723 HMO (719 by word, 4 by the `SAL` family), 127 EOA, 6 POS. No EPO-only plans; 0 unknown.
- **1 PPO rate file**, MIXED, with `requires_in_file_network_filter = True`.

**Hypotheses tested:**
- A PPO-only file exists: rejected.
- The Knox-Keene legal-entity split: not testable, since every plan's issuer is "Healthnet, LLC" and HIOS 67138 covers both HMO and PPO.
- EIN means group, HIOS means individual: rejected; 308 group plans are typed HIOS.
- The in-file `network_name`: empty.

**Outputs:** `outputs/c2/plan_classification.csv`, `file_classification.csv`, `ppo_plans.csv` (456), `ppo_files.csv`, `exclusions_summary.csv`, `conflicts.csv`, `c2_stats.json`.

## 6. C4: rate extraction and selection (done)

Full logic: [logic_c4.md](logic_c4.md).

**Extraction (`ptd_reader.py`).**
- A resumable download to `raw/ptd/`, then two ijson streaming passes (`provider_references`, then `in_network`).
- Batched Parquet writing (250k rows, zstd). About **10 s and 430 MB peak** for the 702 MB file.
- Rates are stored **per price × provider group** (`rates.parquet`), plus group → NPI/TIN (`provider_refs.parquet`). The NPI join happens later, in DuckDB, which keeps storage small.

| Count | Value |
|---|---|
| Codes (`in_network` items) | 1,717 |
| Negotiated rates | 397,726 |
| Prices (rows) | 1,500,288 |
| Provider groups / provider rows | 2,825 / 5,672 |
| NPIs / TINs | 3,735 / 2,888 |
| Invalid records | 0 |

These counts match a direct text count of the raw file.

**What the file looks like.**
- 99.7% of prices are `RC` (revenue code) institutional outpatient rates. There are only 3,535 CPT and 446 HCPCS prices.
- **The real CPT/HCPCS code sits in `additional_information` for 75% of prices.** So every row gets a derived `procedure_code`: the billing code if its type is CPT/HCPCS/DRG, otherwise a CPT/HCPCS-shaped `additional_information`.
- Every expiration date is 9999-12-31.

**No in-file PPO filter.** `network_name` is empty for all 5,672 provider rows, and only 3% of groups carry a `PPO:` or `Direct Network:` business-name label. All rates are kept as the PPO network's rates, and the label is kept as `network_hint`.

**Context** (what "one code for one provider" means): NPI · TIN · arrangement · code type · code · procedure code · service name · modifier · billing class · setting · place of service. Different services are never merged.

**Selection rule,** recorded in `selection_rule`:
1. Drop unusable rates: invalid (null, ≤ $0, placeholder), expired, or not a dollar type (percentage, per diem). The definitions are the same as C3's.
2. Prefer negotiated > fee schedule > derived.
3. Set aside rates more than 10× above or below their group median, **only if a non-extreme rate remains**.
4. Keep the **most frequent** rate.
5. On a tie, keep the **lower median** (position ⌈n/2⌉ of the sorted tied values).

Every row carries `n_candidate_rows`, `n_distinct_rates`, `rate_min`, `rate_max`, `has_conflict`, `selected_support`, `n_tied`, `provider_group_ids`, `group_median` and `network_hint`.

**Result:** **840,574 selected rates** for 3,735 NPIs / 2,888 TINs / 3,780 NPI–TIN pairs, across 775 billing codes and 1,073 procedure codes.

| Selection rule | Contexts |
|---|---|
| single rate | 835,715 |
| lower median of tied rates | 3,032 |
| most frequent rate | 1,708 |
| outliers set aside (+ single or tie) | 84 + 35 |

0 rows dropped; **4,859 contexts had several rates**.

**Outputs:** `outputs/c4/<file_key>/rates.parquet`, `provider_refs*.parquet`, `file_header.json`, **`rates_selected.parquet`** (the deliverable), `selection_summary.csv`; `outputs/c4/c4_stats.json`. Here `file_key` = `5bf4afa2cef9bc2a`.

## 7. C3: file quality grading (done)

Full logic: [logic_c3.md](logic_c3.md).

**Files graded:** the PPO rate file (13 metrics) and all 11 index files (5 metrics).

**Scoring shapes:**
- share = the share of good records;
- tolerance = max(0, 1 − bad share ÷ tolerance);
- freshness = 1 up to 31 days, falling linearly to 0 at 365 days.

The total is the weighted mean of the scored metrics; metrics that can't be scored are left out and the weights rescaled. Grades: A ≥ 0.90, B ≥ 0.80, C ≥ 0.70, D ≥ 0.60, else F.

The **as-of date** is the latest date seen in the inputs (2026-10-06), not the run date, so reruns are reproducible.

| Metric | How | Weight | CA file raw → score |
|---|---|---|---|
| schema_validity | Parsed records × rows with all required fields | 1.0 | 1.000 → 1.00 |
| code_identifiability | Rows with a standard procedure code | 1.0 | 0.752 → 0.75 |
| referential_integrity | Rows whose provider group resolves to an NPI | 1.0 | 1.000 → 1.00 |
| code_coverage | Benchmark-weighted share of the 583 HiLabs codes with a usable **professional** rate | 2.0 | 0.681 → 0.68 (134 codes; any class: 203 codes, 0.753) |
| specialty_code_coverage | Benchmark weight of (specialty, code) cells priced by an NPI of that specialty (NPPES taxonomy → HiLabs crosswalk) | 1.5 | 0.493 → 0.49 |
| provider_validity | NPIs that pass the check digit, are in NPPES, aren't deactivated and are in CA (NPPES primary, NPPES secondary, or DAC) | 1.5 | 0.967 → 0.97 (3,611 of 3,735) |
| zero_invalid_rates | Null, ≤ $0 or placeholder rows (tolerance 10%) | 1.5 | 0 → 1.00 |
| outlier_rate | Rates more than 10× from their group median (tolerance 5%) | 1.5 | 0.0156 → 0.69 |
| rate_type_usability | Rows with a dollar rate type | 1.0 | 1.000 → 1.00 |
| exact_duplicates | 1 − distinct ÷ rows (tolerance 50%) | 0.5 | 0.0085 → 0.98 |
| conflicting_rates | NPI contexts with more than one rate (tolerance 10%) | 1.0 | 0.0050 → 0.95 |
| file_freshness | Days from `last_updated_on` to the as-of date | 1.0 | 0 → 1.00 |
| expired_rates | Rows past `expiration_date` (tolerance 10%) | 1.0 | 0 → 1.00 |

**CA rate file total: 0.857, grade B** (all 13 metrics scored).

**Index files:** healthnet A (0.931), ambetter B (0.814), fidelis C (0.778), QualChoice base D (0.649, because every plan's ID type conflicts with its market), QualChoice sub-indexes C (0.778, except hot_springs A).

**Findings:**
- The file is mostly facility rates.
- Procedure codes sit in a free-text field.
- Outliers concentrate in RC 23 (home-health episodes: median $121.63, range $1.45–$7,380.48).
- The URL date (09-30) doesn't match the content date (10-06).
- 3,054 Type 2 vs 672 Type 1 NPIs.
- NPPES is stale: 2,290 of 3,733 NPIs (61%) haven't been updated since 2023.
- Only the CA-only DAC is available here.

**Outputs:** `outputs/c3/file_scores.csv`, `metric_values.csv`, `metric_details.json`, `index_file_scores.csv`, `c3_stats.json`.

## 7b. C5: provider addresses

Full logic: [logic_c5.md](logic_c5.md).

**Sources** (weight, family):

| Source | Weight | Family | Notes |
|---|---|---|---|
| CA licensed-facility list, by NPI | 0.8 | `ca_cdph` | **High trust: verifies alone** |
| CA licensed-facility list, by name + ZIP | 0.6 | `ca_cdph` | |
| CMS DAC (national file), by NPI | 0.7 | `cms_dac` | |
| CMS DAC group practices (organisation name ≈ DAC group name, same ZIP) | 0.6 | `cms_dac` | Confirms organisations |
| CMS POS CLIA sites (Q3 2023, active; organisation or clinician name ≈ site name, same ZIP) | 0.5 | `cms_clia` | Clinics, physician offices, labs |
| NPPES primary | 0.5 × recency | `nppes` | |
| NPPES secondary | 0.4 × recency | `nppes` | |
| IRS EO (CA) | 0.4 | `irs` | Linked by TIN = EIN |

Recency factor: within 1 year 1.0, 1–3 years 0.7, older 0.4. The downloaded files live in `raw/sources/` (`health_facility_locations.csv`, `eo_ca.csv`, and `POS_CLIA_Q3_2023.csv`, which the user downloaded manually). The national DAC file is in `reference/` (git-ignored).

**Name matching** (one shared rule for every name + ZIP source): same ZIP, word overlap ≥ 0.6 and **at least 2 shared meaningful words**, after removing legal and generic words, stemming plurals, and removing the site's city and county words plus generic place words. The place-word guard fixed "Santa Maria"-type false matches. The 2-word guard fixed "Aurora Medical Corporation" vs "Aurora Medical Center" and removed 155 single-word verifications. A name match only adds a candidate; verification still needs the same street address from another family.

**Method:**
- **Address key** = normalised street (suite removed, street words abbreviated) + ZIP5.
- **Confidence** = 1 − Π(1 − weight).
- **Verified** = in CA and (≥ 2 independent families agree, or on the CA licence register by NPI).
- **Best address order:** in CA → verified → confidence → recency.

**TIN–NPI–address link.** Each pair's addresses are re-ranked by TIN-specific evidence: the IRS address of the TIN, the DAC group name matching the TIN's business name, or the NPPES organisation name matching it.

**Census geocoding** (`src/geocode.py`, batch API, cached in `outputs/_cache/geocode/`):
- standardises every candidate address, so the same site spelt differently merges (355 NPI addresses merged);
- confirms existence (93% of best addresses), and adds coordinates and county.

It isn't an independent source. Its version is accepted only if the house number is unchanged and the match is exact or the ZIP is unchanged; 34 approximate matches were rejected after an audit found street and city swaps.

**County labels:** the `PPO: <County>` labels agree with the geocoded county for 82 of 92 NPIs (89%), which is evidence for Q4.

**Health Net directory** (searchable, not downloadable) is used as a **manual accuracy check**. `outputs/c5/directory_check_sample.csv` holds 25 verified + 25 unverified NPIs with blank columns for a person to fill in. C5 reads it back (never overwriting it) and reports precision in `c5_stats.json → directory_check`. No scraping.

**Results:**
- 3,727 of 3,735 NPIs have an address; 3,613 in CA.
- **1,899 verified (50.8%)**: 247 individuals and 1,652 organisations. 1,887 by two sources, 12 by the state register alone.
- History: 216 (NPPES + DAC) → 698 (+ CA register, IRS) → 1,719 (+ DAC group practices, another session) → 1,564 (2-word guard) → 1,813 (+ CLIA) → **1,899** (+ geocoder merging spellings).
- TINs verified: 1,435 of 2,888 (49.7%).
- 1,564 of 3,780 pairs (41%) have TIN-specific evidence.
- Status counts: unverified single source 1,345, unverified multiple 368, out of state only 114, deactivated 7, no address 2.

**Outputs:** `outputs/c5/address_candidates.parquet`, `npi_addresses.csv`, `provider_locations.csv`, `tin_npi_address.csv`, `c5_stats.json`.

**Could add:**
- the Health Net provider directory (the best remaining source);
- the CMS POS hospital/non-hospital extract (the user supplied the CLIA extract);
- the CA Secretary of State business registry;
- filling in the directory spot check.

See `logic_c5.md` §7 for what each remaining group needs.

## 7c. C6: provider rate file

Full logic: [logic_c6.md](logic_c6.md).

**How rows are built:** C4 `rates_selected` (840,574) × the C5 TIN–NPI address list × NPPES name and taxonomy → NUCC specialty (plus the HiLabs specialty in the Parquet file). For each pair: every verified address, else the best candidate, else a `no_address` row. The rate repeats across a pair's addresses (TiC has no site-level pricing).

**Result:**
- **1,466,669 rows**; 3,734 NPIs; 2,888 TINs.
- **97.0% of rows have a verified address**: verified 1,422,786, unverified 42,887, out of state 947, no address 46, deactivated 3.
- 5 rows with NPI 0 are excluded from the Excel file (kept in Parquet with `npi_valid = false`).

**Excel:** sheets About, Summary, Rates 1 (1,048,575 rows), Rates 2 (418,094). Sheets split at an NPI boundary after 1M rows, or at the hard limit. Written by `xlsxwriter` in constant-memory mode in about 60 s. The file is 103 MB, so it's git-ignored; the Parquet file (2 MB) is committed.

**Summary (deliverable 4):** `outputs/SUMMARY.md` and `outputs/summary.json`.

**Spot check:** stopped after 12 of 50 at the user's request; results in `logic_c5.md` §7. The directory also confirmed EOA = Elect Open Access (POS).

## 8. Reference-data loaders (`src/reference_data.py`)

- **Benchmark workbook** with three sheets:
  - `Spec-Proc Weights`: 946 rows, 583 codes, all 5 characters, leading zeros kept;
  - `Specialty Weights`: 48 rows;
  - `Taxonomy-Spec Xwalk`: 213 rows.
- **NUCC:** unique codes.
- **NPPES, practice locations and DAC are loaded by NPI scope, not by state.** The scope is the NPIs in `outputs/c4/*/provider_refs.parquet`; before C4 exists it falls back to the states CA, NV, OR, AZ (`config/stages/c5.yaml`). Every state and every status is kept, which lets C3 and C5 tell invalid, deactivated and out-of-state NPIs apart.
- **`nppes_status`** is `active`, `deactivated` or `reactivated` (reactivated when the reactivation date is on or after the deactivation date).
- **Caches** are written to the ignored `outputs/_cache/reference/` and named by scope.
- **DAC fallback:** without the national DAC file, the committed CA-only `dac_ca.parquet` is used and labelled `dac_source = legacy_ca_only`.
- **`npi_is_valid`** is the NPI check digit (Luhn with prefix 80840).
- **NPPES for the 3,735 NPIs:** 3,733 found; active 3,716, reactivated 10, deactivated 7. States: CA 3,601, OR 21, WA 11, NV 9, TX 8, others. Practice locations: 1,663 rows for 467 NPIs. DAC (CA only): 486 rows for 327 NPIs.

## 9. Git state

| Branch | Contents |
|---|---|
| `main` | C1, C2, scaffold, `work_split.md` (milestone branches), and the teammate's original reference loaders (commits `901eac9`, `2a25193` by `nsb5257`) |
| `feat/reference-data` | Fixes to the reference loaders (NPI scope, status column, check digit, caches, tests) |
| `chore/add-duckdb` | Adds duckdb to `requirements.txt` |
| `feat/quality-c3` | Everything above plus C3, C4 extraction and C4 selection, and the docs |
| `feat/location-c5` | **`feat/quality-c3` + C5 (first version). The latest branch.** `main` can fast-forward to it |

The user asked to **keep the work on the branch and not touch `main`** until told otherwise. No PR has been opened; the `gh` CLI isn't installed.

Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## 10. Open questions for the organizers (`open_questions.md`)

- **Answered:** Q1 (EOA = separate network) and Q2 (PPO+EPO = PPO).
- **Open:**
  - Q3: is a PPO-only rate file expected, or should PPO rates be filtered inside the shared CA file?
  - Q4: are the `PPO:` business-name labels real network tags?
  - Q5: plan-name abbreviations.
  - Q6: is `CUSPPO` a typo for `CUS PPO`?
  - Q7: plan ID type conflicts.
  - Q8: is there a plan-to-licensed-entity mapping?
  - Q9: why do two QualChoice sub-indexes duplicate each other?

## 11. Conventions and preferences

- Code style: concise, **no comments or docstrings**, streaming everywhere. DuckDB for large aggregations. Deterministic outputs (sorted rows, stable hashes, as-of date from the data).
- Never inflate numbers. Report exact counts, including unknowns and conflicts. Anything ambiguous goes in `open_questions.md`, never a silent guess.
- Every exclusion or decision carries a reason code. No ML for classification; rules must be explainable to judges.
- The user writes in short informal messages and wants plain-language explanations for the organizers. Keep docs updated with real numbers.

## 12. What's next

**Scalability improvements** (judged under "Scalability", 40 points): see [scalability_review.md](scalability_review.md) for the gap analysis and a prioritised to-do list (about half a day for the recommended set).

1. **C5 optional extras:** Census batch geocoding and the Health Net directory. The CMS POS file was skipped (see logic_c5 §7). Each plugs in as a new source with its own weight.
2. **Remaining deliverables:** the video (≤ 5 min), the final README pass, refreshing the shared summary doc and Word copy with the C3–C6 numbers, and optionally C7 and a UI.
3. **The summary file and the shared summary doc** (claude.ai artifact `https://claude.ai/code/artifact/acdaa8c8-b12a-4364-8f9b-d25bb3d3dd88`) need C3, C4, C5 and C6 numbers. A refreshed Word copy goes in `docs/`.
4. **Optional:** C7 cost-effectiveness metrics, a UI, and the video.
