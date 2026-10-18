# C6: the provider rate file

**Result.** `outputs/c6/provider_rate_file.xlsx` holds **1,466,669 rows**, one per **NPI × TIN × practice address × billing code**, with the columns the brief asks for (NPI, TIN, Address, Billing Code, Specialty, Rate) plus the context that makes each row unique.
- **97.0% of rows (1,422,786) carry a verified address.**
- 3,734 NPIs, 2,888 TINs (the 5 excluded rows belong to TINs that also have valid NPIs).
- 3,726 NPIs have a specialty.

`outputs/SUMMARY.md` (and `summary.json`) is deliverable 4, the high-level metrics from the whole run.

Reproduce (needs C4 and C5 outputs):

```bash
.venv/bin/python -m src.run --stage c6
```

It takes about 60 s.

**About the Excel file:** it's **103 MB**, above GitHub's 100 MB per-file limit, so it's git-ignored and rebuilt by the command above. `outputs/c6/provider_rate_file.parquet` (2 MB, committed) holds the same rows plus every evidence column.

## 1. How a row is built

```
rates_selected (C4)  ── one rate per NPI × TIN × code × context
        ×
TIN-NPI address list (C5) ── every verified address for the pair, or the best candidate if none is verified
        ×
provider attributes ── NPPES name, taxonomy → NUCC specialty (and the HiLabs specialty where the crosswalk maps it)
```

All three joins run in DuckDB and stream to Parquet. The Excel workbook is then written from the Parquet file in batches (`xlsxwriter`, constant-memory mode), so memory stays flat.

1. **Rates (C4).** 840,574 selected rates. Each is the single rate kept for one provider context (NPI, TIN, code type, code, procedure code, service name, billing class, setting, place of service, modifier, arrangement) by the C4 rule. The rule is explained in `logic_c4.md`.
2. **Addresses (C5, `tin_npi_address.csv`).** For each TIN–NPI pair:
    - **if the pair has any verified address, every verified address is kept**, because a provider can work at several sites;
    - **otherwise the single best candidate is kept** (rank 1 for that pair);
    - **if there's no address at all, the row is still kept** with status `no_address`.

    Unverified alternative candidates are left out of the deliverable, which avoids filling it with stale NPPES sites. They stay in `outputs/c5/npi_addresses.csv`.
3. **Specialty.** The NPI's NPPES taxonomy, translated to its NUCC display name (e.g. "Ambulatory Surgical Clinic/Center"). The Parquet file also carries the HiLabs benchmark specialty from the crosswalk, available for 2,089 NPIs.

**The rate repeats across addresses.** A TiC rate file prices a provider per TIN, not per site, so a pair with three verified addresses gets the same rate on three rows. 47 is the largest number of verified addresses for one pair (a multi-site organisation).

**Rows left out.** 5 rows whose NPI is 0 (TIN-only contracts in the rate file; they fail the NPI check digit) aren't in the Excel file. They're kept in the Parquet file with `npi_valid = false`.

## 2. Columns

| Excel column | Meaning |
|---|---|
| NPI, TIN | From the PPO rate file's provider groups |
| Provider Name | NPPES organisation name (Type 2), or first + last name (Type 1) |
| Address, City, State, ZIP | Practice address for this TIN–NPI pair (C5) |
| Address Status | `verified` (2 independent sources, or the CA licence register), `unverified` (best candidate only), `out_of_state`, `deactivated_npi`, `no_address` |
| Billing Code Type, Billing Code | As published: CPT, HCPCS or RC (revenue code) |
| Procedure Code | CPT/HCPCS for the service, taken from the billing code or from `additional_information` on revenue-code rows (78% of rows have one) |
| Service Name | The description published with the code |
| Billing Class, Place of Service, Modifier | The context C4 keeps apart. 1,448,313 rows are institutional and 18,356 professional |
| Specialty | NUCC display name of the NPI's taxonomy |
| Rate, Rate Type | The selected rate and its type (negotiated / fee schedule / derived) |

The **Parquet file** adds:
- address evidence: `address_confidence`, `address_sources`, `verification_basis`, `link_evidence`, `pair_rank`, county, latitude, longitude;
- rate evidence: `rate_min`, `rate_max`, `n_candidate_rows`, `n_distinct_rates`, `selection_rule`;
- `hilabs_spec_cd`, `hilabs_specialty`, `network_hint`, `npi_valid`.

## 3. Rows by address status

| Status | Rows |
|---|---|
| verified | 1,422,786 |
| unverified | 42,887 |
| out_of_state | 947 |
| no_address | 46 |
| deactivated_npi | 3 |

## 4. Sheets
- **About:** grain, sources, exclusions, and what each column means.
- **Summary:** the same metrics as `outputs/SUMMARY.md`.
- **Rates 1 … n:** the rows. Excel's limit is 1,048,576 rows per sheet. A new sheet starts at an NPI boundary once a sheet passes 1,000,000 rows, or at the hard limit if a single NPI is that large. On this run: 1,048,575 + 418,094.

## 5. Limitations
- **Rates aren't filtered to PPO-only providers inside the shared CA file** (C4, open question Q3). A partial check found several rate-file organisations that the PPO directory doesn't list (`logic_c5.md` §7).
- **The rate repeats across addresses** because the source has no site-level pricing.
- **Unverified addresses (3% of rows) are best candidates, not confirmed.** Their status column says so.
