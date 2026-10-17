# C3: grading PTD file quality and completeness

**Result.** The Health Net CA in-network file (the only PPO rate file) scores **0.887, grade B**, on the 11 metrics that can be scored today. Its weak points:

- **Benchmark coverage:** professional rates cover 23% of the benchmark codes (68% of their weight).
- **Procedure-code identifiability:** 25% of prices carry only a revenue code.
- **Outliers:** 1.56% of prices.

The two NPPES-based metrics are wired up and tested, but they need the NPPES file in `raw/nppes/`. Until then they are reported as *not scored*, never guessed. All 11 index files are graded too.

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

Rates are stored **once per provider group**, not once per NPI. That keeps the extract small. Metrics that need NPI-level detail join the two tables inside DuckDB, which works out of core, so the design scales to files many times this size. The extracted counts were cross-checked against the raw file: 1,717 `billing_code` keys, 1,500,288 `negotiated_rate` keys, 2,825 `provider_group_id` keys.

## 2. What the file looked like (what shaped the metrics)

- **99.7% of prices are institutional outpatient rates keyed by a revenue code (`RC`).** Only 3,535 CPT and 446 HCPCS prices exist.
- **75% of prices put the actual procedure code (CPT/HCPCS) in the free-text `additional_information` field.** For example, RC `636` with `J0690`. A grader that only reads `billing_code` would see almost no procedures, and an outlier check grouped by revenue code would compare different drugs with each other. So:
    - every metric derives a **`procedure_code`**: `billing_code` when the type is CPT/HCPCS/DRG, else a CPT/HCPCS-shaped `additional_information`;
    - outlier groups also include the procedure code and service name;
    - a dedicated metric measures how much of the file is identifiable at all.
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
| 2 | `code_identifiability` (validity) | Price rows with a standard procedure code (as defined in §2) ÷ all rows | A bare revenue code (RC 636 "drugs") can't be compared across payers or providers | share | 1.0 | 0.752 → 0.75 |
| 3 | `referential_integrity` (validity) | Price rows whose provider group resolves to at least one NPI ÷ all rows | A rate that can't be tied to a provider is useless for provider-level rates | share | 1.0 | 1.000 → 1.00 |
| 4 | `code_coverage` (completeness) | Benchmark weight of codes with at least one usable **professional** rate ÷ total benchmark weight. Code weight = Σ over specialties of specialty weight × (code weight ÷ that specialty's total). Usable means > $0, not a placeholder, a dollar rate type, and not expired | A file only supports benchmarking if it prices the codes that drive spend. Weighting stops rare codes from counting as much as common ones. The benchmark covers *individual* providers, so facility rates don't count | share | 2.0 | 0.681 → 0.68 (134 of 583 codes; any billing class: 203 codes, 0.753) |
| 5 | `specialty_code_coverage` (completeness) | Benchmark weight of (specialty, code) cells where an NPI of that specialty (NPPES taxonomy → HiLabs crosswalk) has a usable rate for that code ÷ total weight | Contracting compares like with like: a cardiology code priced only for non-cardiologists doesn't support a cardiology benchmark | share | 1.5 | not scored: NPPES needed |
| 6 | `provider_validity` (completeness) | Distinct NPIs that pass the check digit, **and** are in NPPES, **and** are not deactivated, **and** practise in CA (NPPES primary or secondary address, or DAC) ÷ distinct NPIs | Invalid, retired or out-of-state NPIs inflate the network and attach rates to providers a CA team can't use | share | 1.5 | not scored: NPPES needed (check digit: 3,734 of 3,735 valid) |
| 7 | `zero_invalid_rates` (accuracy) | Rows with a null rate, a rate ≤ $0, or a placeholder (0.01, 99999, 999999, 9999999) ÷ all rows | These aren't prices, and they drag averages down | tolerance 10% | 1.5 | 0.000 → 1.00 |
| 8 | `outlier_rate` (accuracy) | Positive rates more than 10× above or below their group median ÷ positive rates in groups of at least 5. A group is code type + code + procedure code + service name + billing class + rate type | Order-of-magnitude gaps usually mean unit errors (cents vs dollars, per unit vs per case) or misattributed codes. A 10× band on a ratio scale ignores normal price spread | tolerance 5% | 1.5 | 0.0156 → 0.69 (22,499 of 1,444,075) |
| 9 | `rate_type_usability` (accuracy) | Rows whose `negotiated_type` is negotiated, fee schedule or derived ÷ all rows | Percentage-of-charges and per-diem rates can't be turned into dollars without a base the file doesn't give | share | 1.0 | 1.000 → 1.00 (46 fee-schedule, rest negotiated) |
| 10 | `exact_duplicates` (uniqueness) | 1 − distinct rows ÷ rows, comparing provider group, code, name, modifier, class, setting, place of service, type, rate, expiry and additional info | Duplicates inflate counts but don't change prices, so they're tolerated generously | tolerance 50% | 0.5 | 0.0085 → 0.98 (12,732 duplicate rows) |
| 11 | `conflicting_rates` (uniqueness) | NPI-level contexts (NPI, TIN, code, name, modifier, class, setting, place of service, type, additional info) with more than one distinct rate ÷ all contexts | Two prices for the same service and provider make the "true" rate ambiguous; C4 needs a rule for these | tolerance 10% | 1.0 | 0.0050 → 0.95 (4,194 of 841,729) |
| 12 | `file_freshness` (timeliness) | Days from the file's `last_updated_on` to the as-of date | TiC requires monthly refreshes; old files misstate current contracts | freshness | 1.0 | 0 days → 1.00 |
| 13 | `expired_rates` (timeliness) | Rows whose `expiration_date` is before the as-of date (9999-12-31 counts as open-ended) ÷ all rows | Expired prices aren't in force | tolerance 10% | 1.0 | 0.000 → 1.00 |

**As-of date.** It's the latest date seen in the inputs: the index snapshot dates and each rate file's `last_updated_on`. Here that's 2026-10-06. Using the run date instead would make reruns non-reproducible. It can be pinned with `c3.as_of_date`.

**Weights.** Coverage gets the most weight (2.0) because a file that doesn't price the benchmark codes can't serve the use case, however clean it is. Accuracy and provider validity get 1.5 because they decide whether a price can be trusted and attributed. Exact duplicates get the least (0.5) because they're harmless once removed.

## 4. Findings the metrics surfaced

1. **The file is mostly facility rates.** Professional rates cover 134 of 583 benchmark codes (68% of benchmark weight). Counting facility rates as well gives 203 codes (75%). Professional benchmarking will be thin for codes such as `27447` (knee replacement), `27130` (hip), `43239` (endoscopy), behavioural health `H0015` and `H2020`, and drugs `J0177` and `J2350` (see `top_missing_codes`).
2. **Procedure codes in a free-text field.** Without the `additional_information` fallback, code identifiability would be 0.3%, not 75%. 372,595 RC prices (25%) have no procedure code at all.
3. **Outliers concentrate in revenue code `23` (home-health episode groups).** One group's median is $121.63, yet its rates span $1.45 to $7,380.48. That looks like a mix of per-visit and per-episode pricing.
4. **Conflicts.** 4,194 provider contexts carry several rates. The widest is RC `370` (anesthesia) for one NPI and TIN: $233.81 vs $2,391.92 (examples in `metric_details.json`).
5. **Metadata.** The file URL says 2026-09-30, but its `last_updated_on` is 2026-10-06, after the index that points to it. That's recorded as `url_date_matches_content = false`.
6. **NPIs.** 3,734 of 3,735 pass the NPI check digit. NPPES status and location come once the NPPES file is added.

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
- **NPPES metrics.** `provider_validity` and `specialty_code_coverage` need `raw/nppes/npidata_pfile_*.csv` (and `pl_pfile`). Until then they're not scored, and the total uses the other 11 metrics.
- **No external price reference.** Outliers are measured against the file's own medians. A Medicare fee schedule would add an external check but isn't among the provided data.
- **Rates aren't filtered to PPO plans.** All CA networks share this file and nothing in it ties a price to a network, so the metrics describe the file as a whole (see open question Q3).
