# C4: extracting PPO rates for every provider

**Result.** **271,053 selected rates for 3,716 NPIs and 2,873 TINs** (3,761 NPI–TIN pairs), across 768 billing codes.

Three decisions shape this number:
1. **Revenue-code rates copied across service names are collapsed.** The file publishes 1,500,288 price rows, but 1,041,529 of them (69%) only repeat a rate set that the same provider already has, under another item name.
2. **Rows with in-file HMO evidence are left out.** That removes 34 provider groups, 91 NPIs and 49 TINs from at least some rates.
3. **One rate is kept per provider context** by a fixed rule. 1,681 contexts had several rates.

Every selected row records which steps fired and the range of rates it chose from.

Reproduce:

```bash
.venv/bin/python -m src.run --stage c4
```

The first run downloads the file (about 700 MB) to `raw/ptd/`. Later runs reuse the extract while its `extract_version` matches the code (`c4.reuse_extract: true`).

## 1. Which file, and the PPO carve-out inside it

**The file comes from C2.** `outputs/c2/ppo_files.csv` lists one file, `2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json`. All 456 PPO plans point to it, and so do the HMO, EOA and POS plans: the same file serves every Health Net CA group network.

Rates in a TiC in-network file are not tied to plans. So the only way to carve PPO out of it is evidence inside the file:

| Evidence | What C4 does | Why | CA file |
|---|---|---|---|
| `negotiation_arrangement = capitation` | Left out (`non_ppo_capitation`) | Capitation (a fixed amount per member) is an HMO payment model. PPO contracts pay per service | 46 prices, 19 provider groups |
| TIN business name labelled HMO and not PPO (e.g. `DIRECT NETWORK: AMBETTER HMO`) | Left out (`non_ppo_label`) | The file itself says this group is in an HMO network | 15 provider groups |
| TIN business name labelled PPO (e.g. `PPO: Los Angeles County`) | Kept, `network_label = ppo_label` | Positive PPO evidence | 75 provider groups |
| Other label (`Direct Network: San Diego`) | Kept, `other_label` | No network type stated | |
| No label | Kept, `unlabelled` | Nothing in the file says otherwise | Most groups |

`provider_references[].network_name` is empty for every provider, so it can't be used.

- **What was removed:** all rows in `outputs/c4/<file_key>/non_ppo_exclusions.csv` (label, NPI, TIN, provider groups, business name, rows). 407 NPI-level rows were dropped: 34 groups, 91 NPIs, 49 TINs. 19 NPI–TIN pairs had only such rows, so they leave the network entirely; the others keep their remaining rates.
- **Switches:** `config/stages/c4.yaml` → `exclude_non_ppo_evidence`, `non_ppo_arrangements`, `ppo_label_pattern`, `non_ppo_label_pattern`.
- **Residual risk:** unlabelled providers who are only in HMO/EOA/POS networks stay in. Nothing in the public file can separate them; the organizers have the question as Q3.

## 2. Extraction (`src/ptd_reader.py`)

1. **Download.** A resumable HTTP download to `raw/ptd/`, with 3 retries and backoff. A resume is trusted only when the server answers `206 Partial Content`; otherwise the file is fetched again from byte 0. The result is checked against the server's size.
2. **Stream with ijson** (the C-based `yajl2_c` backend), never loading the file whole.
    - Plain and **gzip-compressed** files are handled. Compression is detected from the file's magic bytes, so the name doesn't matter.
    - `provider_references` that point by URL to a separate file are fetched once and cached.
    - The two passes (`provider_references` → `provider_refs.parquet`, and `in_network` → `rates.parquet`) run **in parallel** for one file.
    - With several files, **files are extracted in parallel**, one per worker (`--workers`).
3. **Written in batches** of 250k rows with zstd compression, so memory stays flat: about 7 s for the whole file.
4. **Bad records are counted by reason** in `file_header.json` and never stop the run. Reasons: a code missing, a non-numeric rate, a rate with no provider, a group with no NPI, a non-numeric NPI (inline groups included), an unreachable remote provider reference. The CA file had none.

The item's `description` is now extracted as well (in addition to `name`).

| Extraction count | Value |
|---|---|
| `in_network` items (codes) | 1,717 |
| `negotiated_rates` | 397,726 |
| Price rows | 1,500,288 |
| Provider groups / provider rows | 2,825 / 5,672 |
| Distinct NPIs / TINs | 3,735 / 2,888 |
| Invalid records | 0 |

## 3. Revenue-code rates copied across service names

The CA file is 99.7% revenue-code (`RC`) facility rates, and it lists each revenue code once per service name. For RC 300 (laboratory), for example, there are 70 items: "LIPID PANEL", "ASSAY OF FERRITIN", "ROUTINE VENIPUNCTURE" and so on. For a given provider, every one of those items carries **the same set of rates**: the rate doesn't depend on the service name.

The `additional_information` field on these rows holds a CPT/HCPCS-shaped code, but it is usually not the item's own service. For example, "ASSAY OF SEX HORMONE GLOBUL" carries `36415` (venipuncture). Of RC rows whose code is priced elsewhere in the file as a CPT/HCPCS, only 2.4% have the matching item name.

So, in `c3_quality.connect_rates` (shared with C3):
- A **cell** is one provider group's prices for one code, `additional_information`, billing class, setting, place of service, modifier, arrangement and rate type, ignoring the item name.
- When a cell has several item names and **every name carries the identical rate set**, the names don't price anything. One copy is kept, `service_key` is blank, and all names go to `service_names` (`n_service_names` counts them).
- When rates differ by name, the name stays part of the key.
- `associated_code` is the code-shaped `additional_information` on non-procedure codes, shown as published.
- `procedure_code` is set only when it really identifies the service: the billing code when it is CPT/HCPCS/DRG, or an associated code whose name in the file matches the item's name.

Effect on the CA file:

| | Before | After |
|---|---|---|
| Price rows | 1,500,288 | 458,759 |
| Repeated copies removed | | 1,041,529 (26,353 cells repeated across an average of 40 names) |
| Prices with a real procedure code | | 7,073 |
| Prices with an associated code only | | 332,242 |

## 4. What counts as "one code for one provider" (the context)

A rate is selected per **context**: NPI · TIN · negotiation arrangement · billing code type · billing code · associated code · service · modifier · billing class · setting · place of service.

The definition is in `src/contracts.py` (`CONTEXT_COLS`); C3's `conflicting_rates` uses the same one. These are kept apart on purpose: professional and institutional rates, settings, places of service, modifiers and different associated codes are different services, not competing prices for one service. The pair NPI + TIN is the provider, because contracts are signed at TIN level and one NPI can bill under several TINs.

## 5. The selection rule (when a context has several rates)

| Step | What happens | Why | CA file |
|---|---|---|---|
| 0. In-file HMO evidence | Removes capitation rows and HMO-only labelled groups (§1) | Not PPO rates | 407 rows |
| 1. Drop unusable rates | Removes rates that are null, ≤ $0, a placeholder (0.01 / 99999 / 999999 / 9999999), expired (before the as-of date; 9999-12-31 = open-ended), or not a dollar rate type | These aren't prices that can be paid or compared today. Same definitions as C3 | 0 rows |
| 2. Prefer the most direct rate type | negotiated > fee schedule > derived | A negotiated rate is the contracted price | 0 contexts mixed types |
| 3. Set aside extreme values | Drops rates more than 10× above or below the median of their group (code type, code, associated code, service, class, rate type; ≥ 5 rates), **only if a non-extreme rate remains** | Usually a unit or per-case vs per-unit error | 109 contexts |
| 4. Keep the most frequent rate | The value with the most price records wins | Repeated listings show the standard contracted price | 357 contexts decided here |
| 5. On a tie, keep the lower median | Among tied values, sorted, takes position ⌈n/2⌉ | A real published rate, deterministic, never systematically the highest or lowest | 1,215 contexts |

`selection_summary.csv`:

| `selection_rule` | Contexts |
|---|---|
| `single_rate` | 269,372 |
| `lower_median_of_tied_rates` | 1,215 |
| `most_frequent_rate` | 357 |
| `outliers_set_aside+single_rate` | 74 |
| `outliers_set_aside+lower_median_of_tied_rates` | 35 |

**Nothing is hidden.** Each selected row also carries:
- `n_candidate_rows`, `n_distinct_rates`, `rate_min`, `rate_max`, `has_conflict`, `selected_support`, `n_tied`;
- `provider_group_ids` (sorted, so reruns are byte-identical);
- `group_median`, `network_hint`, `network_label`;
- `service_names`, `n_service_names`, `associated_code`, `procedure_code`, `description`.

## 6. Outputs (`outputs/c4/`)

| File | Contents |
|---|---|
| `<file_key>/rates.parquet` | All published prices, per provider group (nothing dropped) |
| `<file_key>/provider_refs.parquet`, `provider_refs_inline.parquet` | Provider group → NPI, TIN, business name |
| `<file_key>/file_header.json` | File metadata and counts, invalid records by reason, `compressed`, `extract_version` |
| `<file_key>/rates_selected.parquet` | **The C4 deliverable:** one rate per provider context, with its selection evidence |
| `<file_key>/non_ppo_exclusions.csv` | What the in-file PPO carve-out removed |
| `<file_key>/selection_summary.csv` | Contexts by selection rule |
| `c4_stats.json` | Extraction and selection counts, dropped rows by reason, conflict examples |

## 7. Checks
- One row per context. Every selected rate lies within its context's `rate_min`–`rate_max`.
- Independent re-derivation of 20 stratified rows from the raw JSON matched the rule 20 of 20 (`review_notes/03_execution_and_verification.md`).
- `tests/test_c4.py` covers each selection step on a hand-built file, plus:
    - the collapse of rates repeated across names;
    - the exclusion of capitation and HMO-labelled groups;
    - gzip input;
    - a non-numeric inline NPI being counted, not fatal.

## 8. Limitations
- **PPO carve-out is partial** (§1): unlabelled providers who are in-network only for HMO/EOA/POS stay in.
- **Revenue-code rates are not procedure prices.** 26% of distinct prices have neither a procedure nor an associated code, and most of the rest have only an associated code whose meaning is unconfirmed (open question Q10).
- **Outliers use the file's own medians.** There is no external fee schedule.
