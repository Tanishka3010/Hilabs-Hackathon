# C6: the provider rate file

**Result.** `outputs/c6/provider_rate_file.xlsx` holds **474,967 rows** in one sheet (39 MB). There is one row per NPI × TIN × practice address × billing code × rate context. The columns the brief asks for (NPI, TIN, Address, Billing Code, Specialty, Rate) come first, and the context that makes each row unique is in its own columns.
- 3,715 NPIs and 2,873 TINs; 3,707 NPIs have a specialty (their **primary** NPPES taxonomy).
- **Address status by NPI** (the headline, from C5):
    - 43.3% have a verified address;
    - 57.8% are verified or corroborated.

  Most rows belong to large multi-site organisations, so the row shares are much higher (75% verified, 97% verified or corroborated). The run summary therefore leads with the NPI and TIN shares.

`outputs/SUMMARY.md` (and `summary.json`) is deliverable 4, the high-level metrics from the whole run.

Reproduce (needs C4 and C5 outputs):

```bash
.venv/bin/python -m src.run --stage c6
```

It takes about 20 s.

## 1. How a row is built

```
rates_selected (C4)  ── one rate per NPI × TIN × code × context (copies across service names already collapsed; HMO evidence removed)
        ×
TIN-NPI address list (C5) ── every verified or corroborated address for the pair, or the best candidate if none
        ×
provider attributes ── NPPES name, primary taxonomy → NUCC specialty (and the HiLabs specialty where the crosswalk maps it)
```

All three joins run in DuckDB (with a memory cap and a spill directory) and stream to Parquet. The Excel workbook is then written from the Parquet file in column batches (`xlsxwriter`, constant-memory mode).

1. **Rates (C4).** 271,053 selected rates. See `logic_c4.md`.
2. **Addresses (C5, `tin_npi_address.csv`).** For each TIN–NPI pair:
    - if it has any verified or corroborated address, all of them are kept, because a provider can work at several sites;
    - otherwise the single best candidate is kept (rank 1 for that pair);
    - if there is no address at all, the row is still kept with status `no_address`.

    Within a pair, addresses with TIN-specific evidence rank first: the IRS address of the EIN, a DAC group practice matching the TIN name, or another NPI under the same TIN reporting the same address.
3. **Specialty.** The NUCC display name of the NPI's **primary** taxonomy: the slot whose primary switch is Y, not slot 1. For 607 NPIs these differ.

## 2. The grain, and why one NPI-TIN-address-code can have several rows

The brief's grain is NPI × TIN × address × billing code. In this file one billing code can carry several distinct prices for the same provider, and each is a different service or contract term:
- **Revenue codes** are priced per associated code (the `additional_information` code Health Net publishes), so RC 300 has one rate with `36415` and another without;
- **billing class** (professional vs institutional), **setting**, **place of service**, **modifier** (e.g. 26/TC) and **arrangement** each change the price.

So each of these has its own Excel column, and a row is unique on NPI, TIN, address, billing code type, billing code, associated code, service, billing class, setting, place of service, modifier and arrangement.

**Grain check.** C6 checks this on every run and fails if any two rows share every key column. The counts are in `c6_stats.json` → `grain`:

| Count | Value |
|---|---|
| Duplicate keys on the full grain | 0 |
| (NPI, TIN, address, code) keys with several context rows | 7,758 |

**Collapsed service names.** When Health Net repeats one rate set under many item names for a revenue code, the rows are collapsed to one row with `Service Name` = "N services: name | name …". The full list is in the Parquet file (`service_names`).

## 3. Columns

| Excel column | Meaning |
|---|---|
| NPI, TIN | From the PPO rate file's provider groups |
| Provider Name | NPPES organisation name (Type 2), or first + last name (Type 1) |
| Address, City, State, ZIP | Practice address for this TIN–NPI pair (C5) |
| Address Status | `verified`, `corroborated`, `unverified`, `out_of_state`, `deactivated_npi` or `no_address` (defined in `logic_c5.md` §4) |
| Billing Code Type, Billing Code | As published: CPT, HCPCS or RC (revenue code) |
| Procedure Code | CPT/HCPCS that identifies the service: the billing code itself, or an associated code whose name matches the item |
| Associated Code | RC rows: the CPT/HCPCS-shaped `additional_information`, as published (usually not the item's own service) |
| Service Name | The item name, or "N services: …" for collapsed copies |
| Billing Class, Setting, Place of Service, Modifier, Arrangement | The rate context kept apart in C4 |
| Network Label | In-file network evidence: `ppo_label`, `other_label`, `unlabelled` (HMO-labelled and capitation rows are excluded) |
| Specialty | NUCC display name of the primary taxonomy |
| Rate, Rate Type | The selected rate and its type |

The **Parquet file** adds:
- address evidence: `address_confidence`, `address_sources`, `verification_basis`, `link_evidence`, `pair_rank`, county, latitude, longitude;
- rate evidence: `rate_min`, `rate_max`, `n_candidate_rows`, `n_distinct_rates`, `selection_rule`;
- `service_names`, `n_service_names`, `description`;
- `hilabs_spec_cd`, `hilabs_specialty`, `network_hint`, `network_label`, `npi_valid`.

## 4. Rows by address status

| Status | Rows |
|---|---|
| verified | 354,711 |
| corroborated | 104,046 |
| unverified | 15,259 |
| out_of_state | 902 |
| no_address | 46 |
| deactivated_npi | 3 |

**Rows left out:** 5 rows whose NPI is 0 (TIN-only contracts in the rate file; they fail the NPI check digit) aren't in the Excel file. They're kept in the Parquet file with `npi_valid = false`.

## 5. Sheets
- **About:** grain, the revenue-code collapse, the PPO filter, sources, exclusions, and what each column means.
- **Summary:** the same metrics as `outputs/SUMMARY.md`.
- **Rates 1 … n:** the rows. A new sheet starts at an NPI boundary once a sheet passes 1,000,000 rows, or at Excel's hard limit. This run fits in one sheet.

## 6. Limitations
- **PPO carve-out is partial** (`logic_c4.md` §1).
- **The rate repeats across addresses** because the source has no site-level pricing.
- **Corroborated and unverified addresses are weaker evidence.** Their status column says so.
