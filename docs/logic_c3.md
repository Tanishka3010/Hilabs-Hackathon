# C3: grading PTD file quality and completeness

**Result.** The Health Net CA in-network file (the only PPO rate file) scores **0.765, grade C**, on all 14 metrics. Its weak points:

- **Rate replication:** 69% of price rows only repeat, under another service name, a rate set the provider already has for that revenue code. The file looks granular, but it holds 458,759 real prices, not 1.5 million.
- **Procedure-code identifiability:** only 1.5% of distinct prices are tied to a procedure that identifies the service. Revenue-code rows carry a CPT/HCPCS-shaped code in `additional_information`, but it usually is not the item's own service.
- **Specialty-matched coverage:** 50% of benchmark weight is priced by a provider of the right specialty.
- **Benchmark coverage:** professional rates cover 23% of the benchmark codes (68% of their weight).
- **Outliers:** 1.3% of distinct prices.

Providers are sound: 96.7% of NPIs are valid, active and located in CA. All 11 index files are graded too. If the NPPES file is missing, the two NPPES-based metrics are reported as *not scored* (never guessed), and the total uses the other 12. With several rate files, they are graded in parallel (`--workers`).

Reproduce:

```bash
.venv/bin/python -m src.run --stage c4
```
```bash
.venv/bin/python -m src.run --stage c3
```

Outputs are in `outputs/c3/`:
- `file_scores.csv`: one row per rate file, with the total, grade and every metric score.
- `metric_values.csv`: one row per file and metric, with the raw value, score, weight, definition and reason.
- `metric_details.json`: the evidence behind each score, such as top outlier groups, conflict examples and missing codes.
- `index_file_scores.csv`
- `c3_stats.json`

## 1. What is graded

| File | Why it's graded |
|---|---|
| `healthnet-ca_in-network.json` (702 MB, the PPO rate file from C2) | The rates for C4 to C6 come from it |
| The 11 index files | C1 and C2 depend on them. "Score every file you use" includes them |

Each rate file is streamed once (`src/ptd_reader.py`, ijson) into three tables under `outputs/c4/<file_key>/`:
- `rates.parquet`: one row per price × provider group. That's 1,500,288 rows, extracted in about 10 s with about 430 MB peak memory.
- `provider_refs.parquet`: provider group → NPI and TIN. 2,825 groups, 3,735 NPIs, 2,888 TINs.
- `file_header.json`: counts and invalid records by reason.

Every metric reads one of two views of the same extract (`c3_quality.connect_rates`, shared with C4):
- **published rows** (`rates_raw`), for listing-level checks: parse errors, zero rates, rate types, expiry, exact duplicates;
- **distinct prices** (`rates`), where copies of one rate set repeated under several service names are collapsed (see `logic_c4.md` §3), for everything about prices: outliers, conflicts, identifiability and coverage.

Rates are stored **once per provider group**, not once per NPI. That keeps the extract small. Metrics that need NPI-level detail join the two tables inside DuckDB, which works out of core, so the design scales to files many times this size. The extracted counts were cross-checked against the raw file: 1,717 `billing_code` keys, 1,500,288 `negotiated_rate` keys, 2,825 `provider_group_id` keys.

## 2. What the file looked like (what shaped the metrics)

- **99.7% of prices are institutional outpatient rates keyed by a revenue code (`RC`).** Only 3,535 CPT and 446 HCPCS prices exist.
- **75% of price rows carry a CPT/HCPCS-shaped code in the free-text `additional_information` field, but it is usually not the item's service.** An item named "ASSAY OF SEX HORMONE GLOBUL" (RC 300) carries `36415` (venipuncture). Of RC rows whose code is priced elsewhere in the file as CPT/HCPCS, the item name matches that code's name only 2.4% of the time. These rates are also far above the file's own professional rates for the same code (median ratio about 49×). So:
    - the code is kept as an **`associated_code`**, as published;
    - **`procedure_code`** is set only when it identifies the service: `billing_code` for CPT/HCPCS/DRG, or an associated code whose name in the file matches the item;
    - outlier and conflict groups include the associated code and the service;
    - two metrics measure this: `code_identifiability` and `rate_replication`.
- **The same rate set is repeated under many service names.** For one provider, RC 300 lists 70 laboratory items, all with identical rates.
- Every `expiration_date` is `9999-12-31` (open-ended), and all but 46 prices are `negotiated`.

## 3. The metrics

Each metric produces a **raw value** (always reported) and a **score from 0 to 1**. There are three scoring shapes, chosen per metric:

| Shape | Formula | Used when |
|---|---|---|
| Share | score = share of good records | the good share is meaningful by itself (e.g. coverage) |
| Tolerance | score = max(0, 1 − bad share ÷ tolerance) | a small bad share is already serious; the tolerance is the share at which the score reaches 0 |
| Freshness | 1 up to 31 days old, falling linearly to 0 at 365 days | age of the file |

The total is the **weighted mean of the scored metrics** (weights in `config/stages/c3.yaml`). Metrics that can't be scored are left out, and the remaining weights are rescaled; `metrics_scored` says how many counted. Grades: A ≥ 0.90, B ≥ 0.80, C ≥ 0.70, D ≥ 0.60, otherwise F.

| # | Metric (group) | How it's calculated | Why it matters | Scoring | Weight | CA file raw → score |
|---|---|---|---|---|---|---|
| 1 | `schema_validity` (validity) | Records that parse ÷ all records, × rows with code, code type, rate, rate type, billing class and expiry ÷ all rows | A record that can't be parsed, or lacks a required field, can't be priced | share | 1.0 | 1.000 → 1.00 |
| 2 | `code_identifiability` (validity) | Distinct prices with a procedure code that identifies the service (see §2) ÷ distinct prices | A bare revenue code (RC 636 "drugs") can't be compared across payers or providers; a code that doesn't describe the item would mislabel the price | share | 1.0 | 0.015 → 0.02 (7,073 of 458,759) |
| 3 | `referential_integrity` (validity) | Price rows whose provider group resolves to at least one NPI ÷ all rows | A rate that can't be tied to a provider is useless for provider-level rates | share | 1.0 | 1.000 → 1.00 |
| 4 | `code_coverage` (completeness) | Benchmark weight of codes with at least one usable **professional** rate ÷ total benchmark weight. Code weight = Σ over specialties of specialty weight × (code weight ÷ that specialty's total). Usable means > $0, not a placeholder, a dollar rate type, and not expired | A file only supports benchmarking if it prices the codes that drive spend. Weighting stops rare codes from counting as much as common ones. The benchmark covers *individual* providers, so facility rates don't count | share | 2.0 | 0.681 → 0.68 (134 of 583 codes; any billing class: 203 codes, 0.753) |
| 5 | `specialty_code_coverage` (completeness) | Benchmark weight of (specialty, code) cells where an NPI of that specialty (NPPES **primary** taxonomy → HiLabs crosswalk) has a usable rate for that code ÷ total weight | Contracting compares like with like: a cardiology code priced only for non-cardiologists doesn't support a cardiology benchmark | share | 1.5 | 0.500 → 0.50 (2,136 NPIs map to a benchmark specialty) |
| 6 | `provider_validity` (completeness) | Distinct NPIs that pass the check digit, **and** are in NPPES, **and** are not deactivated, **and** practise in CA (NPPES primary or secondary address, or DAC) ÷ distinct NPIs | Invalid, retired or out-of-state NPIs inflate the network and attach rates to providers a CA team can't use | share | 1.5 | 0.967 → 0.97 (3,611 of 3,735 usable: 1 fails the check digit, 2 not in NPPES, 7 deactivated, 124 not located in CA by any source) |
| 7 | `zero_invalid_rates` (accuracy) | Rows with a null rate, a rate ≤ $0, or a placeholder (0.01, 99999, 999999, 9999999) ÷ all rows | These aren't prices, and they drag averages down | tolerance 10% | 1.5 | 0.000 → 1.00 |
| 8 | `outlier_rate` (accuracy) | Distinct positive prices more than 10× above or below their group median ÷ prices in groups of at least 5. A group is code type + code + associated code + service + billing class + rate type | Order-of-magnitude gaps usually mean unit errors (cents vs dollars, per unit vs per case) or misattributed codes. A 10× band on a ratio scale ignores normal price spread | tolerance 5% | 1.5 | 0.0128 → 0.74 (5,703 of 444,837) |
| 9 | `rate_type_usability` (accuracy) | Rows whose `negotiated_type` is negotiated, fee schedule or derived ÷ all rows | Percentage-of-charges and per-diem rates can't be turned into dollars without a base the file doesn't give | share | 1.0 | 1.000 → 1.00 (46 fee-schedule, rest negotiated) |
| 10 | `exact_duplicates` (uniqueness) | 1 − distinct rows ÷ rows, comparing provider group, code, name, modifier, class, setting, place of service, type, rate, expiry and additional info | Duplicates inflate counts but don't change prices, so they're tolerated generously | tolerance 50% | 0.5 | 0.0085 → 0.98 (12,732 duplicate rows) |
| 10b | `rate_replication` (uniqueness) | Published rows that only repeat, under another service name, the rate set the same provider group already has for that code, associated code and context ÷ published rows | When one rate set is copied across dozens of service names, the names don't price anything: the file looks granular but holds far fewer real prices, and per-service benchmarks built on it would be wrong | tolerance 50% | 1.0 | 0.694 → 0.00 (1,041,529 of 1,500,288) |
| 11 | `conflicting_rates` (uniqueness) | Provider contexts with more than one distinct rate ÷ all contexts. The context is the C4 one (`src/contracts.py`: NPI, TIN, arrangement, code, associated code, service, modifier, class, setting, place of service), so C3 and C4 count conflicts the same way | Two prices for the same service and provider make the "true" rate ambiguous; C4 needs a rule for these | tolerance 10% | 1.0 | 0.0062 → 0.94 (1,694 of 271,445; C4 finds 1,681 after removing HMO-evidence rows) |
| 12 | `file_freshness` (timeliness) | Days from the file's `last_updated_on` to the as-of date | TiC requires monthly refreshes; old files misstate current contracts | freshness | 1.0 | 0 days → 1.00 |
| 13 | `expired_rates` (timeliness) | Rows whose `expiration_date` is before the as-of date (9999-12-31 counts as open-ended) ÷ all rows | Expired prices aren't in force | tolerance 10% | 1.0 | 0.000 → 1.00 |

**As-of date.** It's the latest date seen in the inputs: the index snapshot dates and each rate file's `last_updated_on`. Here that's 2026-10-06. Using the run date instead would make reruns non-reproducible. It can be pinned with `c3.as_of_date`.

**Weights.** Coverage gets the most weight (2.0) because a file that doesn't price the benchmark codes can't serve the use case, however clean it is. Accuracy and provider validity get 1.5 because they decide whether a price can be trusted and attributed. Exact duplicates get the least (0.5) because they're harmless once removed. Rate replication gets 1.0: unlike exact duplicates, it misleads anyone who reads the file per service.

## 4. Findings the metrics surfaced

1. **The file is mostly facility rates.** Professional rates cover 134 of 583 benchmark codes (68% of benchmark weight). Facility (revenue-code) rates add no codes once only real procedure codes count; the codes they carry in `additional_information` would have added 69, but they don't describe the priced service (§2). Professional benchmarking will be thin for codes such as `27447` (knee replacement), `27130` (hip), `43239` (endoscopy), behavioural health `H0015` and `H2020`, and drugs `J0177` and `J2350` (see `top_missing_codes`).
2. **Revenue-code prices are not procedure prices.** Of 458,759 distinct prices, 7,073 (1.5%) have a procedure code that identifies the service, 332,242 carry only an associated code, and the rest have neither. Reading `additional_information` as the procedure (the earlier version of this pipeline) would have reported 75% and priced venipuncture at a median of $7,990.
3. **Outliers concentrate in revenue code `23` (home-health episode groups).** 5,703 of 444,837 eligible prices (1.3%) are outliers. One group's median is $121.63, yet its rates span $1.45 to $7,380.48. That looks like a mix of per-visit and per-episode pricing.
4. **Conflicts.** 1,694 provider contexts carry several rates. The widest is RC `23` (home health) for one NPI and TIN: $241.34 to $7,380.48 across 6 rates (examples in `metric_details.json`).
5. **Metadata.** The file URL says 2026-09-30, but its `last_updated_on` is 2026-10-06, after the index that points to it. That's recorded as `url_date_matches_content = false`.
6. **Providers.**
    - 3,733 of 3,735 NPIs exist in NPPES; 7 are deactivated and 10 reactivated.
    - The NPPES primary address is CA for 3,601. The others are in OR (21), WA (11), NV (9), TX (8) and elsewhere. Adding NPPES secondary locations and DAC brings the CA-located count to 3,611.
    - The file is dominated by **organisations**: 3,054 Type 2 NPIs vs 672 Type 1. That fits its facility-rate profile.
7. **Specialty-matched coverage is about half the benchmark weight (0.50, using each NPI's primary taxonomy).** Several specialties get no specialty-matched coverage at all, including Cardiothoracic Surgery (035) and the dental specialties (201–203). This file can't support a specialty-level benchmark for them.
8. **Rate replication.** 1,041,529 rows (69%) repeat a rate set under another service name: 26,353 cells, on average 40 names each.
9. **NPPES addresses are stale, as the brief warns.** 1,211 of the 3,733 NPIs were last updated in 2019 or earlier, and 2,290 (61%) have had no update since 2023. C5 has to use more than NPPES (see `last_update_date` in the reference cache).

## 5. Index files (also used, also graded)

The metrics are on the same 0–1 scale, with weights in `c3.index_weights`:

| Metric | How it's calculated | Why |
|---|---|---|
| `index_schema_completeness` | Plan entries with name, ID, ID type, market and file URL ÷ entries | C1 can't place a plan without these |
| `index_id_consistency` | Plans without a C2 ID flag (ID type vs ID shape vs market) ÷ plans | Contradictory IDs make plan identity and market unreliable |
| `index_network_type_stated` | Plans whose name states PPO/HMO/EPO/POS/EOA ÷ plans | The problem statement notes names rarely say the network; this measures how much C2 had to infer |
| `index_duplicate_entries` | 1 − repeated (plan, file) entries ÷ entries | Repeats inflate plan counts |
| `index_freshness` | Index date vs the as-of date (freshness scale) | Same reason as for rate files |

| Index file | Entries | Score | Grade |
|---|---|---|---|
| healthnet | 1,693 | 0.931 | A |
| ambetter | 1,120 | 0.814 | B |
| qualchoice (base) | 308 | 0.649 | D (every plan has an ID type that conflicts with its market) |
| fidelis | 94 | 0.778 | C |
| QualChoice employer sub-indexes (6 files) | 1–32 | 0.778 | C (no names state a network type) |
| qualchoice_city_of_hot_springs | 2 | 1.000 | A |

## 6. Limitations
- **NPPES metrics.** `provider_validity` and `specialty_code_coverage` need `raw/nppes/npidata_pfile_*.csv` (and `pl_pfile`). The first run streams 9.85 million NPPES rows (about 30 s) and caches the 3,733 that matter.
- **DAC is CA-only here.** The national DAC file isn't in the repo, so the committed CA-only cache is used. DAC can confirm a CA location, but it can't reveal an out-of-state DAC address.
- **No external price reference.** Outliers are measured against the file's own medians. A Medicare fee schedule would add an external check but isn't among the provided data.
- **The metrics describe the whole file.** All CA networks share it; C4 removes rows with in-file HMO evidence afterwards (34 provider groups). `metric_details.json` → `network_labels` reports how much of the file carries network labels (75 groups labelled PPO, 15 HMO-only, 46 capitation prices).
- **Meaning of `additional_information` is unconfirmed** (open question Q10). If Health Net confirms it is the procedure, identifiability would be about 75%.
