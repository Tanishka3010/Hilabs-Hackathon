# C4: extracting PPO rates for every provider

**Result.** Every provider in the PPO rate file has rates: **840,574 selected rates for 3,735 NPIs and 2,888 TINs** (3,780 NPI–TIN pairs), across 775 billing codes and 1,073 procedure codes.

Most combinations of provider and service have one rate. **4,859 have several**, and each is reduced to one rate by a fixed rule: drop unusable rates → prefer the most direct rate type → set aside extreme values → keep the most frequent rate → on a tie, keep the lower median. Every selected row records which steps fired and the range of rates it chose from.

Reproduce:

```bash
.venv/bin/python -m src.run --stage c4
```

The first run downloads the file (about 700 MB) to `raw/ptd/`. Later runs reuse the extract (`c4.reuse_extract: true`).

## 1. Which file and which rates

- **The file comes from C2:** `outputs/c2/ppo_files.csv` lists one file, `2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json`. All 456 PPO plans point to it.
- **There is no PPO-only subset inside it.** The same file serves every Health Net CA group network (PPO, HMO, EOA, POS). Rates in a TiC in-network file aren't tied to plans. Checked across the whole file:
    - `provider_references[].network_name` is empty for all 5,672 provider rows;
    - only 96 of 2,825 provider groups (3%) carry a business-name label: 68 `PPO: <County>` and 28 `Direct Network: …`, 21 of which say `AMBETTER PPO`/`AMBETTER HMO`.

  That's far too sparse to filter on. So **all rates in the file are kept as the PPO network's rates**, and the label is kept as `network_hint` on each row, present on 273 selected rows. This is open question Q3 for the organizers.

## 2. Extraction (`src/ptd_reader.py`)

1. **Download.** A resumable HTTP download (Range requests) to `raw/ptd/`, checked against the server's size. The local copy was verified as 701,615,514 bytes, valid JSON from end to end, and 8 random 64 KB chunks identical to the live file.
2. **Stream with ijson** (the C-based `yajl2_c` backend), never loading the file whole. Two passes:
    - `provider_references` → `provider_refs.parquet`: one row per provider group × NPI, with TIN, TIN type, business name and network name.
    - `in_network` → `rates.parquet`: one row per **price × provider group**. Provider groups listed inline in a rate get synthetic `inline-…` IDs and go to `provider_refs_inline.parquet`.
3. **Written in batches** of 250k rows with zstd compression, so memory stays flat: 430 MB peak, about 10 s for the whole file.
4. **Bad records are counted by reason** in `file_header.json`: a code missing, a non-numeric rate, a rate with no provider, a group with no NPI, a non-numeric NPI. The CA file had none.

Rates are stored per **provider group**, not per NPI. Duplicating every price for every NPI in its group would inflate the stored data. The NPI join happens later, inside DuckDB.

| Extraction count | Value |
|---|---|
| `in_network` items (codes) | 1,717 |
| `negotiated_rates` | 397,726 |
| Prices (rows in `rates.parquet`) | 1,500,288 |
| Provider groups / provider rows | 2,825 / 5,672 |
| Distinct NPIs / TINs | 3,735 / 2,888 |
| Invalid records | 0 |

These counts match a direct text count of the raw file: 1,717 `billing_code`, 1,500,288 `negotiated_rate` and 2,825 `provider_group_id` keys.

**Procedure code.** 99.7% of prices are revenue-code (`RC`) facility rates. For 75% of all prices the real CPT/HCPCS code is in `additional_information`. Every row therefore gets a derived `procedure_code`: the billing code when its type is CPT/HCPCS/DRG, otherwise a CPT/HCPCS-shaped `additional_information`. This is the same rule C3 uses.

## 3. What counts as "one code for one provider" (the context)

A rate is selected per **context**:

> NPI · TIN · negotiation arrangement · billing code type · billing code · procedure code · service name · modifier · billing class · setting · place of service

These are kept apart on purpose. Professional and institutional rates, inpatient and outpatient settings, different places of service, modifiers (e.g. 26/TC) and different procedures under one revenue code are **different services**, not competing prices for one service. Merging them would compare unlike things. The pair NPI + TIN is the provider, because contracts are signed at TIN level and one NPI can bill under several TINs.

Joining the 1,500,288 prices to their NPIs gives 1,511,720 rows. Those fall into 840,574 contexts.

## 4. The selection rule (when a context has several rates)

The steps run in order, and each one is recorded in the row's `selection_rule`:

| Step | What happens | Why | CA file |
|---|---|---|---|
| 1. Drop unusable rates | Removes rates that are null, ≤ $0, a placeholder (0.01 / 99999 / 999999 / 9999999), expired (`expiration_date` before the as-of date; 9999-12-31 = open-ended), or not a dollar rate type (percentage, per diem). The count by reason is in `dropped_rows` | These aren't prices that can be paid or compared today. The definitions are identical to C3's, so grading and extraction agree | 0 rows dropped |
| 2. Prefer the most direct rate type | Keeps only the best type present: negotiated > fee schedule > derived | A negotiated rate is the contracted price; fee-schedule and derived rates are proxies | 0 contexts mixed types |
| 3. Set aside extreme values | Within the remaining rates, drops any more than 10× above or below the median of its group (code type, code, procedure, name, class, rate type; groups of ≥ 5), **but only if a non-extreme rate remains** | A 10× gap usually means a unit or per-case vs per-unit error, so it shouldn't win the vote. Keeping it when it's the only rate avoids silently losing a provider | 119 contexts |
| 4. Keep the most frequent rate | Counts how many price records support each remaining value; the most-supported value wins | Repeated listings of the same value show the provider's standard contracted price | 1,708 contexts decided here |
| 5. On a tie, keep the lower median | Among tied values, sorted, takes position ⌈n/2⌉ (e.g. $10 of {10, 20}; $20 of {10, 20, 30}) | It's a real published rate, unlike an average. It's robust, deterministic, and never systematically the highest or lowest | 3,032 contexts |

`selection_summary.csv`:

| `selection_rule` | Contexts | Price rows behind them |
|---|---|---|
| `single_rate` (one distinct value, possibly listed several times) | 835,715 | 1,492,344 |
| `lower_median_of_tied_rates` | 3,032 | 10,459 |
| `most_frequent_rate` | 1,708 | 7,941 |
| `outliers_set_aside+single_rate` | 84 | 748 |
| `outliers_set_aside+lower_median_of_tied_rates` | 35 | 228 |

**Nothing is hidden.** Each selected row also carries:
- `n_candidate_rows` and `n_distinct_rates`;
- `rate_min` and `rate_max` of all usable rates;
- `has_conflict`;
- `selected_support` and `n_tied`;
- the `provider_group_ids` behind the chosen value;
- `group_median` and `network_hint`.

A reviewer can always see what was given up. For example, NPI 1184722779, RC 370 "Anesthesia General": $1,188.52 was kept, and $209.95 (more than 10× below its group median) was set aside.

## 5. Outputs (`outputs/c4/`)

| File | Contents |
|---|---|
| `<file_key>/rates.parquet` | All prices, per provider group (the full extract, nothing dropped) |
| `<file_key>/provider_refs.parquet`, `provider_refs_inline.parquet` | Provider group → NPI, TIN, business name |
| `<file_key>/file_header.json` | File metadata and counts, plus invalid records by reason |
| `<file_key>/rates_selected.parquet` | **The C4 deliverable:** one rate per provider context, with its selection evidence |
| `<file_key>/selection_summary.csv` | Contexts by selection rule |
| `c4_stats.json` | Extraction and selection counts, dropped rows, conflict examples |

## 6. Checks
- One row per context: 840,574 rows, 840,574 distinct contexts.
- Every selected rate lies within its context's `rate_min`–`rate_max`.
- `tests/test_c4.py` covers each step on a hand-built file: most frequent wins; ties of 2 and 3 take the lower median; negotiated beats derived; the outlier is set aside only when an alternative exists; $0, expired and percentage rates are dropped with reasons; a context whose only rate is invalid produces no row.

## 7. Limitations
- **Not PPO-specific inside the file** (see §1): the rates are the CA commercial-group file's rates.
- **Outliers use the file's own medians.** There's no external fee schedule to compare against.
- **Revenue-code rows without a procedure code** (25% of prices) are kept and keyed by revenue code plus service name. They can't be compared on CPT/HCPCS.
