# C5: mapping PTD TINs and NPIs to their practising address

**Result.** Every NPI in the PPO rate file is placed:
- **3,727 of 3,735 have a candidate address**, and 3,613 have one in CA;
- **1,813 NPIs (48.5%) have a verified address**: 231 individual clinicians and 1,582 organisations;
- **1,370 of 2,888 TINs (47.4%)** have at least one verified address;
- **1,564 of 3,780 NPI–TIN pairs (41%)** have evidence tying the address to that specific TIN.

Eight sources are combined and scored, and nothing relies on NPPES alone. An address is verified only when two independent sources agree, or when California's licence register confirms it.

Reproduce:

```bash
.venv/bin/python -m src.run --stage c5
```

It runs in about 15–20 s once the reference caches exist. It needs:
- `raw/nppes/` (NPPES);
- `reference/DAC_NationalDownloadableFile.csv`;
- in `raw/sources/`: `health_facility_locations.csv`, `eo_ca.csv` and `POS_CLIA_*.csv`.

Any missing source is skipped with a warning.

## 1. The relationship being built

> **TIN → NPI** comes from the rate file: each provider group lists NPIs under one TIN. **NPI → address** comes from eight external sources. **TIN → address** is the union of its NPIs' addresses, ranked per pair.

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
| CA facility list, matched by name + ZIP (facilities with no NPI listed) | `ca_cdph` | 0.6 | Organisation name ≈ facility name **and** same ZIP | 76 (56 NPIs) | Same register, but the link is inferred, so not high trust |
| **CMS DAC** (national file) | `cms_dac` | 0.7 | NPI | 498 | Refreshed from Medicare enrollment (PECOS). Clinicians only. Carries the **group practice name** per address |
| **CMS DAC group practices** (all 314,486 CA DAC rows) | `cms_dac` | 0.6 | Organisation name ≈ DAC group practice name **and** same ZIP | 3,587 (1,045 organisations) | DAC lists, for every clinician, the group practice they bill through and its site. A group's DAC sites are Medicare-enrolment addresses of the **organisation itself**, independent of NPPES. The link is a name match, so it's weighted below a direct NPI match |
| **CMS POS CLIA file** (Q3 2023; active certificates only: 33,500 sites in our NPIs' ZIPs, from 628,095 rows) | `cms_clia` | 0.5 | Organisation name, **or a clinician's first + last name**, ≈ CLIA site name **and** same ZIP | 1,158 (817 NPIs) | Every site that runs lab tests, including clinics and physician offices, needs a CLIA certificate for its physical address. It's an independent federal register. Weighted lower because the file is from 2023 and the link is a name match |
| NPPES primary practice address | `nppes` | 0.5 × recency | NPI | 3,726 | Universal but self-reported; the brief says about 80% are stale |
| NPPES secondary practice locations | `nppes` | 0.4 × recency | NPI | 1,663 | Same registry, so not independent of the primary |
| **IRS exempt organisations, CA** (irs.gov, posted 13 Oct 2026) | `irs` | 0.4 | **TIN = EIN** (142 of our TINs) | 372 | The filing address, often the headquarters rather than a practice site. It can confirm an address, never verify one alone |

**About the POS file.** The user supplied the CLIA extract of the Provider of Services file, not the "hospital and non-hospital facilities" extract. data.cms.gov blocks scripted downloads from here. For CA, the hospital/non-hospital facilities are already covered by the CDPH register (with CCN and NPI). The CLIA file adds what was missing: **clinics, physician offices and labs**, which make up most of the remaining unverified organisations.

**Recency** adjusts NPPES trust using `Last Update Date`, relative to the as-of date (2026-10-06):

| Last updated | Factor |
|---|---|
| Within 1 year | 1.0 |
| 1 to 3 years ago | 0.7 |
| More than 3 years ago | 0.4 |

61% of these NPIs haven't been updated since 2023.

**Name matching.** Every name + ZIP source (CA register, DAC groups, CLIA) uses **one shared rule**. A match needs **all** of:
1. the same ZIP as one of the NPI's NPPES addresses;
2. word overlap (Jaccard) ≥ 0.6;
3. **at least 2 shared meaningful words**.

Before comparing, the names are cleaned:
- legal and generic words are removed (INC, LLC, MEDICAL, GROUP, CENTER, CORPORATION…), as are labels like `PPO:`;
- singular and plural forms are treated as the same word;
- **the site's own city (and county) words** and generic place words (COUNTY, VALLEY, SAN, SANTA…) are removed.

Each guard was added after a spot check failed:
- **Place words:** "Endoscopy Center of **Santa Maria**" had matched "**Santa Maria** Health Center". Removing place words cut CA-register name matches from 180 to 81.
- **Two shared words:** "**Aurora** Medical Corporation" had matched "**Aurora** Medical Center", because only "Aurora" survives cleaning. Requiring two shared words removed **155 verifications** that rested on a single word (1,719 → 1,564). CLIA then added **249** (→ 1,813).

A name + ZIP match only adds a **candidate**. The address is verified only if it's the **same street address** that another independent source reports for that NPI (§4). That protects against a borderline name match, e.g. "Pacific **Eye** Surgery Center" vs "Pacific Surgery Center" at the same building.

Spot checks of the confirmed CLIA matches show the same organisation or clinician at the same site, e.g.:
- Skyway Surgery Center (Chico);
- North Bay Regional Surgery Center (Novato);
- Providence Medical Institute (Torrance);
- Robert Hartman MD (Encino).

## 3. Matching the same address written differently

Each address is turned into a key:
1. Join the address lines and upper-case them.
2. Strip suite, unit, building and floor parts. These are kept separately as `units`, so suites in one building count as one site.
3. Remove punctuation, and standardise street words and directions (STREET→ST, BOULEVARD→BLVD, NORTH→N, …).
4. Key = normalised street + ZIP5.

## 4. Scoring and verification

For each (NPI, address key):
- **Confidence** = 1 − Π(1 − weight) over the sources reporting it, each source counted once. For example, CA register + a stale NPPES primary gives 1 − 0.2 × 0.8 = 0.84.
- **Verified** = the address is in CA **and** either it's reported by at least **2 independent source families** (`two_independent_sources`: 1,788 NPIs), **or** it's on the CA licence register by NPI (`state_license_register`: 25 NPIs).
- **Best address per NPI**, in order: in CA → verified → confidence → most recent NPPES update → address key.

| Sources behind a verified best address | NPIs |
|---|---|
| DAC group practice + NPPES primary | 675 |
| CLIA + NPPES primary | 222 |
| CA register + NPPES primary | 202 |
| DAC (by NPI) + NPPES primary | 177 |
| CLIA + DAC group practice + NPPES primary | 146 |
| CA register + CLIA + NPPES primary | 78 |
| CLIA + DAC group practice (no NPPES) | 34 |
| CLIA + DAC (by NPI) + NPPES primary | 31 |
| CA register only | 25 |
| Other combinations | 223 |

CLIA is part of 603 verified addresses, and the deciding second source for 230.

`location_status` per NPI:

| Status | Individuals | Organisations | Total |
|---|---|---|---|
| `verified` | 231 | 1,582 | **1,813** |
| `unverified_single_source` | 293 | 1,052 | 1,345 |
| `unverified_multiple_candidates` | 141 | 313 | 454 |
| `out_of_state_only` (e.g. national labs) | 7 | 107 | 114 |
| `deactivated_npi` (NPPES withholds their address) | 7 | 0 | 7 |
| `no_address` (not in NPPES, no other source) | — | — | 2 |

Of the 331 clinicians DAC knows by NPI, about two-thirds agree with NPPES. The rest show the staleness the brief warns about.

## 5. Linking each TIN–NPI pair to an address

For each pair (`tin_npi_address.csv`), the NPI's addresses are re-ranked so **addresses tied to that TIN come first**. Evidence goes in `link_evidence`:

| Evidence | Meaning | Address rows |
|---|---|---|
| `irs_address_for_tin` | The address is the IRS address of **this TIN** (EIN match) | 372 |
| `dac_group_name_matches_tin` | DAC lists this address under a group practice whose name matches the TIN's business name (similarity ≥ 0.5) | 11 |
| `nppes_org_name_matches_tin` | The organisation's NPPES legal name matches the TIN's business name. NPPES hides the EIN (`<UNAVAIL>`), so the name is the bridge | 2,454 |
| `verified_address` | The address itself is verified | 2,428 |
| `npi_address_only` | No TIN-specific evidence; the NPI's own ranking applies | 3,912 |

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

The 1,922 NPIs still unconfirmed are listed below with what each group would need:

| Remaining group | NPIs | What would confirm them |
|---|---|---|
| A single source, almost always NPPES | 1,345 (1,052 organisations) | A second independent address: the **Health Net provider directory** (network-specific, the best source; needs access and terms review), the **CMS POS hospital/non-hospital extract** for certified non-CA-licensed sites, **NPPES-independent business registries** (CA Secretary of State business search, which lists principal addresses by entity name) |
| Several candidates, none confirmed | 454 | The same sources; or **Census geocoding** to discard candidates that don't exist and check counties against the rate file's `PPO: <County>` labels |
| Only out-of-state addresses (national labs, mail-order, telehealth) | 114 | Mostly correct as-is (e.g. LabCorp, Quest headquarters). Confirming a CA site needs the lab's own location list or CLIA certificates filtered to CA by organisation |
| Deactivated / not in NPPES | 9 | Not mappable from public registries; flag for the organizers |

Each new source becomes another `source` with its own weight and family. The rule doesn't change.

## 8. Limitations
- 51.5% of NPIs still have an unverified best address (see §7).
- The CLIA extract is from Q3 2023. A certificate active then may have closed since, so CLIA is weighted lower and never verifies alone.
- The IRS address is a filing address: it raises confidence and links a TIN, but it isn't treated as a practice site on its own.
- Name matching is rule-based (word overlap, place words removed, at least 2 shared words). The thresholds (0.5 for TIN names, 0.6 for site names) are configurable, and every similarity is reported.
- There's no geocoding yet, so agreement between sources is the evidence that an address exists.
