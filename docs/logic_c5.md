# C5: mapping PTD TINs and NPIs to their practising address

**Result.** Every NPI in the PPO rate file is placed:
- **3,727 of 3,735 have a candidate address**, and 3,613 have one in CA;
- **698 NPIs (18.7%) have a verified address**: 219 individual clinicians and 479 organisations;
- **546 of 2,888 TINs (18.9%)** have at least one verified address;
- **1,564 of 3,780 NPI–TIN pairs (41%)** have evidence tying the address to that specific TIN.

Six sources are combined and scored, and nothing relies on NPPES alone. An address is verified only when two independent sources agree, or when California's licence register confirms it.

Reproduce:

```bash
.venv/bin/python -m src.run --stage c5
```

It runs in about 10 s once the reference caches exist. It needs `raw/nppes/` (NPPES), `reference/DAC_NationalDownloadableFile.csv`, and `raw/sources/health_facility_locations.csv` and `eo_ca.csv`. Any missing source is skipped with a warning.

## 1. The relationship being built

> **TIN → NPI** comes from the rate file: each provider group lists NPIs under one TIN. **NPI → address** comes from six external sources. **TIN → address** is the union of its NPIs' addresses, ranked per pair.

The brief warns that one TIN can have many addresses and one NPI can practise at many. So nothing is collapsed early: every (NPI, address) candidate is kept and scored, and every (TIN, NPI) pair gets its own ranked address list.

| Item | Count |
|---|---|
| NPIs | 3,735 (672 individuals, 3,054 organisations, 2 not in NPPES) |
| TINs | 2,888 |
| NPI–TIN pairs | 3,780 |

## 2. Sources

| Source | Family | Weight | Links on | Candidates | Why this weight |
|---|---|---|---|---|---|
| **CA licensed and certified facility list** (CDPH, data.chhs.ca.gov, 16 Oct 2026; active licences only, 14,583 facilities) | `ca_cdph` | **0.8, high trust** | NPI | 561 | California's official licence register, refreshed monthly. A licence is issued for a physical site |
| CA facility list, matched by name + ZIP (facilities with no NPI listed) | `ca_cdph` | 0.6 | NPPES org name ≈ facility name **and** same ZIP | 81 | Same register, but the link is inferred, so not high trust |
| **CMS DAC** (national file) | `cms_dac` | 0.7 | NPI | 498 | Refreshed from Medicare enrollment (PECOS). Clinicians only. Carries the **group practice name** per address |
| NPPES primary practice address | `nppes` | 0.5 × recency | NPI | 3,726 | Universal but self-reported; the brief says about 80% are stale |
| NPPES secondary practice locations | `nppes` | 0.4 × recency | NPI | 1,663 | Same registry, so not independent of the primary |
| **IRS exempt organisations, CA** (irs.gov, posted 13 Oct 2026) | `irs` | 0.4 | **TIN = EIN** (142 of our TINs) | 372 | The filing address, often the headquarters rather than a practice site. It can confirm an address, never verify one alone |

**Recency** adjusts NPPES trust using `Last Update Date`, relative to the as-of date (2026-10-06):

| Last updated | Factor |
|---|---|
| Within 1 year | 1.0 |
| 1 to 3 years ago | 0.7 |
| More than 3 years ago | 0.4 |

61% of these NPIs haven't been updated since 2023.

**Name matching** (facility list, DAC group names, NPPES organisation names) uses word overlap (Jaccard) after:
- removing legal and generic words (INC, LLC, MEDICAL, GROUP, CENTER…) and labels like `PPO:`;
- treating singular and plural forms as the same word;
- for facility matching, also removing **the facility's own city and county words** and generic place words (COUNTY, VALLEY, SAN, SANTA…).

The last step was added after spot checks showed false matches on place names alone, e.g. "Endoscopy Center of **Santa Maria**" vs "**Santa Maria** Health Center". It cut name + ZIP matches from 180 to 81, and the remaining ones are the same organisation (e.g. Sharp Memorial Hospital, Loma Linda Surgical Institute).

## 3. Matching the same address written differently

Each address is turned into a key:
1. Join the address lines and upper-case them.
2. Strip suite, unit, building and floor parts. These are kept separately as `units`, so suites in one building count as one site.
3. Remove punctuation, and standardise street words and directions (STREET→ST, BOULEVARD→BLVD, NORTH→N, …).
4. Key = normalised street + ZIP5.

## 4. Scoring and verification

For each (NPI, address key):
- **Confidence** = 1 − Π(1 − weight) over the sources reporting it, each source counted once. For example, CA register + a stale NPPES primary gives 1 − 0.2 × 0.8 = 0.84.
- **Verified** = the address is in CA **and** either it's reported by at least **2 independent source families** (`two_independent_sources`), **or** it's on the CA licence register by NPI (`state_license_register`).
- **Best address per NPI**, in order: in CA → verified → confidence → most recent NPPES update → address key.

| Sources behind a verified best address | NPIs |
|---|---|
| CA register + NPPES primary | 314 |
| DAC + NPPES primary | 209 |
| CA register + IRS + NPPES primary | 48 |
| CA register (name + ZIP) + NPPES primary | 44 |
| CA register only | 35 |
| IRS + NPPES primary | 25 |
| Other combinations | 23 |

`location_status` per NPI:

| Status | Individuals | Organisations | Total |
|---|---|---|---|
| `verified` | 219 | 479 | **698** |
| `unverified_single_source` | 302 | 2,042 | 2,344 |
| `unverified_multiple_candidates` | 144 | 426 | 570 |
| `out_of_state_only` (e.g. national labs) | 7 | 107 | 114 |
| `deactivated_npi` (NPPES withholds their address) | 7 | 0 | 7 |
| `no_address` (not in NPPES, no other source) | — | — | 2 |

Of the 331 clinicians DAC knows, about two-thirds agree with NPPES. The rest show the staleness the brief warns about.

## 5. Linking each TIN–NPI pair to an address

For each pair (`tin_npi_address.csv`), the NPI's addresses are re-ranked so **addresses tied to that TIN come first**. Evidence goes in `link_evidence`:

| Evidence | Meaning | Address rows |
|---|---|---|
| `irs_address_for_tin` | The address is the IRS address of **this TIN** (EIN match) | 372 |
| `dac_group_name_matches_tin` | DAC lists this address under a group practice whose name matches the TIN's business name (similarity ≥ 0.5) | 11 |
| `nppes_org_name_matches_tin` | The organisation's NPPES legal name matches the TIN's business name. NPPES hides the EIN (`<UNAVAIL>`), so the name is the bridge | 1,968 |
| `verified_address` | The address itself is verified | 779 |
| `npi_address_only` | No TIN-specific evidence; the NPI's own ranking applies | 3,586 |

Rows can carry several kinds of evidence. **1,564 of 3,780 pairs (41%)** have at least one TIN-specific item (IRS, DAC group name or NPPES organisation name).

## 6. Outputs (`outputs/c5/`)

| File | One row per |
|---|---|
| `address_candidates.parquet` | Candidate (NPI × source × address), with normalised key, unit, source name, date and TIN (for IRS) |
| `npi_addresses.csv` | NPI × distinct address: sources, families, confidence, verified, verification basis, rank, DAC and registry names |
| `provider_locations.csv` | NPI: best address, status, confidence, counts, taxonomy |
| `tin_npi_address.csv` | TIN × NPI × address: rank for the pair, link evidence, name similarities |
| `c5_stats.json` | Coverage figures (NPIs and TINs: any, in CA, verified, by basis and entity type) and the config used |

## 7. What would raise coverage further

| Source | Adds | Status |
|---|---|---|
| CMS Provider of Services file | Medicare-certified facility addresses by CCN, including outside CA | data.cms.gov blocks automated download; the user will add it to `raw/sources/` |
| US Census Geocoder (batch API) | Confirms an address exists, standardises it, gives the county, which can be checked against `PPO: <County>` labels | Not yet used |
| Health Net provider directory | Network-specific practising locations | Needs access and terms review |

Each becomes another `source` with its own weight and family. The rule doesn't change.

## 8. Limitations
- 81% of NPIs still have an unverified best address. Most are organisations that are neither on the CA licence register nor nonprofits; physician groups and labs are typical.
- The IRS address is a filing address: it raises confidence and links a TIN, but it isn't treated as a practice site on its own.
- Name matching is rule-based (word overlap with place words removed). The thresholds (0.5 for TIN names, 0.6 for facilities) are configurable, and every similarity is reported.
- There's no geocoding yet, so agreement between sources is the evidence that an address exists.
