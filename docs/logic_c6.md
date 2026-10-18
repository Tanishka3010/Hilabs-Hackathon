# C6: the provider rate file

**Result.** The rate file (deliverable 1) has **31,981 rows, one per NPI × TIN × address × billing code**, with exactly the columns the brief asks for: **NPI, TIN, Address, Billing Code, Specialty, Rate**. It is written three ways:
- `outputs/c6/provider_rate_file.xlsx` (1.2 MB, one sheet);
- `outputs/c6/provider_rate_file.csv`;
- `outputs/c6/provider_rate_file.parquet`.

Every run checks that no two rows share NPI, TIN, address and billing code.

The full detail stays in `outputs/c6/provider_rate_detail.parquet` (474,967 rows). It holds every rate context (associated code, place of service, modifier, setting, arrangement) and the evidence behind each row (address status and sources, selection rule, service names, HiLabs specialty). C7 and the UI read it.

- 3,715 NPIs and 2,873 TINs; specialty is the NUCC name of each NPI's **primary** NPPES taxonomy.
- **Address status** (from C5): 43.3% of NPIs have a verified address and 57.8% a verified or corroborated one. For TINs (linked through their NPIs) it is 49.2% and 57.8%, and 44.9% of TINs have every one of their NPIs verified.

`outputs/SUMMARY.md` (and `summary.json`) is deliverable 4.

Reproduce (needs C4 and C5 outputs):

```bash
.venv/bin/python -m src.run --stage c6
```

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

## 2. The grain: one rate per NPI × TIN × address × billing code

In the detail file one billing code can carry several rates for the same provider. Each is a different service or contract term:
- **revenue codes** are priced per associated code (the `additional_information` code Health Net publishes);
- **place of service**, **modifier**, **setting** and **arrangement** each change the price.

The brief asks for one row per NPI, TIN, address and billing code, so one rate is kept per key by a fixed rule:
1. **Prefer the base contract:** fee-for-service, no associated code, no modifier. These are the terms that apply when nothing special is billed.
2. **Among the rates left, keep the lower median:** position ⌈n/2⌉ of the sorted distinct rates. This is the same tie rule as C4. It is a real published rate and never systematically the highest or lowest.

4,061 of the 31,981 keys had more than one rate before this step; every one of those rates is still in the detail file. The grain check runs on both files: the rate file must be unique on (NPI, TIN, address, billing code), and the detail file on its full context.

**Address.** The full practice address, "street, suite, city, state ZIP". A pair gets every verified or corroborated address it has (a provider can work at several sites), otherwise its best candidate; a pair with no address keeps a blank Address.

## 3. Columns

| Column | Meaning |
|---|---|
| NPI | From the PPO rate file's provider groups |
| TIN | The Tax ID the NPI bills under in the rate file |
| Address | Practice address for this TIN–NPI pair (C5) |
| Billing Code | As published: CPT, HCPCS or revenue code |
| Specialty | NUCC display name of the NPI's primary taxonomy |
| Rate | The rate kept for this key (§2), from the C4 selection |

Everything else (address status, sources, associated code, place of service, rate type, selection rule) is in `provider_rate_detail.parquet`.

## 4. Rows left out

5 detail rows whose NPI is 0 (TIN-only contracts in the rate file) fail the NPI check digit. They are left out of the rate file and kept in the detail file with `npi_valid = false`.

## 5. Sheets
The Excel workbook holds only the rate rows (`Rates 1 … n`). A new sheet starts at an NPI boundary once a sheet passes 1,000,000 rows, or at Excel's hard limit; this run fits in one sheet.

## 6. Limitations
- **PPO carve-out is partial** (`logic_c4.md` §1).
- **The rate repeats across addresses** because the source has no site-level pricing.
- **Corroborated and unverified addresses are weaker evidence.** Their status column says so.
