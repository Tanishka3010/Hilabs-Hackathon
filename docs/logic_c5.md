# C5: mapping PTD TINs and NPIs to their practising address

**Result.** Every NPI in the PPO rate file (after C4's HMO-evidence carve-out: 3,716 NPIs, 2,873 TINs, 3,761 NPI–TIN pairs) is placed:
- **3,708 have a candidate address**, and 3,594 have a best address in CA;
- **661 NPIs (17.8%) have a verified address**: two sources linked to the NPI by an identifier (NPI or EIN) agree, or California's licence register lists it;
- **another 1,225 (33.0%) have a corroborated address**: NPPES agrees with a site that was found by an organisation-name + ZIP search. That brings **verified or corroborated to 50.7%**;
- **510 TINs (17.8%) have a verified address, and 1,389 (48.3%) a verified or corroborated one**. 129 TINs (4.5%) have a verified address that is tied to that TIN specifically;
- **3,463 best addresses (93%) are confirmed to exist** by the US Census geocoder, which also gives coordinates and county;
- **673 of 3,761 NPI–TIN pairs (18%)** have evidence tying the address to that specific TIN: the IRS address of the EIN, a DAC group practice named like the TIN, or another NPI under the same TIN reporting the same address.

Eight sources are combined and scored. 1,599 NPIs (43%) still have a best address supported only by NPPES; those stay `unverified`.

**Why two levels of verification.** A name + ZIP match (DAC group practices, CLIA sites, CDPH facilities without an NPI) is searched for in the ZIP codes NPPES gives for the NPI. When such a site then "agrees" with NPPES, the two sources are not fully independent: the search started from NPPES. Those addresses are reported as *corroborated*, kept in the rate file, and counted separately from *verified*.

Reproduce:

```bash
.venv/bin/python -m src.run --stage c5
```

It runs in about 20 s once the reference and geocoding caches exist. The first run sends about 5,000 distinct addresses to the Census batch geocoder (3 batches, about 90 s). Results are cached in `outputs/_cache/geocode/`, so each address is only sent once; set `c5.geocode.enabled: false` to run offline. It needs:
- `raw/nppes/` (NPPES);
- `reference/DAC_NationalDownloadableFile.csv`;
- in `raw/sources/`: `health_facility_locations.csv`, `eo_ca.csv` and `POS_CLIA_*.csv`.

Every source is required: a missing one stops C5 with its name, because silently skipping it would change the verified share. Set `c5.allow_missing_sources: true` to run without it on purpose; it is then listed in `c5_stats.json` → `missing_sources`.

NPPES, NPPES secondary locations and DAC are read once per run for the rate file's full NPI set, and cached in `outputs/_cache/reference/`; C3, C5 and C6 share that cache. The specialty used everywhere is the NPI's **primary** taxonomy (the slot whose primary switch is Y), not slot 1; for 607 NPIs they differ.

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

### 3b. Census geocoding (standardise, confirm, locate)

Every distinct candidate address (5,066) goes to the [US Census Geocoder batch API](https://geocoding.geo.census.gov/geocoder/) (benchmark `Public_AR_Current`), with the suite removed. The geocoder returns a match status, an official address, coordinates and the county FIPS code. County names come from the CA facility file's FIPS-to-county pairs.

| Census result | Raw addresses | Used for |
|---|---|---|
| Match | 4,671 | The official address becomes the address key, so spellings from different sources merge (355 NPI addresses were merged from several spellings). Coordinates and county are kept |
| No_Match | 342 | Kept unchanged. **No match doesn't mean the address is fake**: the Census street file misses some real addresses (e.g. 22 Corporate Plaza, Newport Beach) |
| Tie | 19 | Kept unchanged |
| Rejected_Non_Exact | 34 | An approximate match that **changed the house number, or changed the ZIP without being exact**. Kept unchanged, and the geocoder's version is discarded |

**The safety rule was added after an audit.** Approximate matches sometimes moved an address to another street and city:
- 600 **N** Garfield Ave, Pasadena 91108 → 600 **S** Garfield Ave, Alhambra 91754;
- 1200 El Camino **Real** 94080 → 1200 El Camino **Del Mar**, San Francisco.

The Census version is now accepted only if **the house number is unchanged** and it's **an exact match or the ZIP is unchanged**. After this rule no merged address changes ZIP. Harmless approximate matches, such as Beverly Blvd → **W** Beverly Blvd, are still accepted.

The geocoder isn't counted as an independent *source*: it confirms an address exists, not that the provider practises there. It's used to merge spellings, to break ranking ties (a geocoded address beats an equally confident one that isn't), and for county and coordinates in C6. Merging raised the NPIs with two agreeing sources from 1,813 to 1,899 (measured under the earlier single-level rule).

**What the county check found** (`county_label_check.csv`). 100 NPIs sit in provider groups the rate file labels `PPO: <County>`. Of the 92 that have a geocoded county, **82 agree (89%)**. That suggests these labels describe the providers' real counties (open question Q4). The 10 that differ are mostly neighbouring counties (Los Angeles vs Riverside or Santa Barbara, San Bernardino vs Los Angeles), and 7 of the 10 are unverified addresses.

## 4. Scoring and verification

For each (NPI, address key):
- **Confidence** = 1 − Π(1 − weight) over the sources reporting it, each source counted once. For example, CA register + a stale NPPES primary gives 1 − 0.2 × 0.8 = 0.84.
- **Identifier-linked vs name-linked sources.**
    - Identifier-linked: NPPES (by NPI), DAC (by NPI), the CA register (by NPI) and IRS (by EIN = TIN).
    - Name-linked: DAC group practices, CLIA sites and CA register sites without an NPI, all matched by name + ZIP (`c5.name_linked_sources`).
- **Verified** (`verification_strength = strict`) = the address is in CA **and** either at least **2 source families with an identifier link** report it (`two_id_linked_sources`), **or** the CA licence register lists it by NPI (`state_license_register`).
- **Corroborated** = in CA, 2 source families agree, but one of them is name-linked (`id_and_name_linked_sources`).
- **Best address per NPI**, in order: in CA → verified → corroborated → confidence → geocoded → most recent NPPES update → address key.

Scoring is done with grouped aggregations (no per-address Python loop).

| Sources behind a verified best address | NPIs |
|---|---|
| CA register + NPPES primary | 199 |
| DAC (by NPI) + NPPES primary | 179 |
| CA register + CLIA + NPPES primary | 94 |
| CLIA + DAC (by NPI) + NPPES primary | 34 |
| CA register + DAC group (+ CLIA) + NPPES primary | 40 |
| CA register + IRS + NPPES primary | 18 |
| IRS + NPPES primary | 16 |
| Other combinations | 81 |

| Sources behind a corroborated best address | NPIs |
|---|---|
| DAC group practice + NPPES primary | 690 |
| CLIA + NPPES primary | 256 |
| CLIA + DAC group practice + NPPES primary | 173 |
| CA register (name + ZIP) + NPPES primary | 21 |
| CLIA + DAC group practice (no NPPES) | 21 |
| Other combinations | 64 |

`location_status` per NPI:

| Status | Individuals | Organisations | Total |
|---|---|---|---|
| `verified` | 223 | 438 | **661** |
| `corroborated` | 13 | 1,212 | **1,225** |
| `unverified_single_source` | 292 | 1,049 | 1,341 |
| `unverified_multiple_candidates` | 123 | 243 | 366 |
| `out_of_state_only` (e.g. national labs) | 7 | 107 | 114 |
| `deactivated_npi` (NPPES withholds their address) | — | — | 7 |
| `no_address` (not in NPPES, no other source) | — | — | 2 |

## 5. Linking each TIN–NPI pair to an address

For each pair (`tin_npi_address.csv`), the NPI's addresses are re-ranked so **addresses tied to that TIN come first**, then by **TIN consensus** (how many NPIs under the same TIN report the address), then by the NPI's own rank. Evidence goes in `link_evidence`:

| Evidence | Meaning | TIN-specific? | Address rows |
|---|---|---|---|
| `irs_address_for_tin` | The address is the IRS address of **this TIN** (EIN match) | yes | 372 |
| `dac_group_name_matches_tin` | DAC lists this address under a group practice whose name matches the TIN's business name (similarity ≥ 0.5) | yes | 11 |
| `shared_by_tin_npis` | At least one other NPI billing under the same TIN reports the same address (TIN consensus) | yes | 1,430 |
| `nppes_org_name_matches_tin` | The organisation's NPPES legal name matches the TIN's business name. It ties the NPI to the TIN, but says nothing about the address | **no** | 2,318 |
| `verified_address` | The address itself is verified or corroborated | no | 2,515 |
| `npi_address_only` | No other evidence; the NPI's own ranking applies | no | 2,821 |

Rows can carry several kinds of evidence. **673 of 3,761 pairs (18%)** have at least one TIN-specific item. TIN-level figures in `c5_stats.json`:
- `tins_with_verified_address`: any NPI under the TIN has a verified address;
- `tins_with_verified_or_corroborated_address`: the same, including corroborated;
- `tins_with_tin_specific_verified_address`: the verified or corroborated address also carries TIN-specific evidence.

## 6. Outputs (`outputs/c5/`)

| File | One row per |
|---|---|
| `address_candidates.parquet` | Candidate (NPI × source × address), with normalised key, unit, source name, date and TIN (for IRS) |
| `npi_addresses.csv` | NPI × distinct address: sources, families, confidence, verified, verification basis, rank, DAC and registry names |
| `provider_locations.csv` | NPI: best address, status, confidence, counts, taxonomy |
| `tin_npi_address.csv` | TIN × NPI × address: rank for the pair, link evidence, name similarities |
| `county_label_check.csv` | NPI in a `PPO: <County>` group: the label, our geocoded county, agrees or differs |

| `c5_stats.json` | Coverage figures (NPIs and TINs: any, in CA, verified, by basis and entity type), geocoding and county-check counts, directory-check precision, and the config used |

`provider_locations.csv` also carries the best address's geocoded form, latitude, longitude and county.

## 7. The Health Net provider directory, and what's left

**The directory can be searched but not downloaded** (search by name, location, specialty and so on; no CSV or API offered). So it's used as an **accuracy check, not a bulk source**. Automated scraping is avoided because the site's terms of use haven't been reviewed, and a public directory isn't meant for bulk extraction.

**The spot check** (`reference/directory_check_sample.csv`; kept with the reference data so a rerun into a fresh output folder never loses the manual checks). C5 writes a fixed random sample of **50 in-CA NPIs**: 25 with a verified address and 25 without (seed 7). Each row has the provider name, NPI, TIN, specialty and our best address, plus blank columns to fill in:
- `directory_found` (Y/N);
- `directory_address`;
- `address_matches` (Y/N: same street address, suites ignored);
- `checked_by`, `checked_on`, `notes`.

A person looks each one up in the Health Net directory and fills the columns.

**Partial result (2026-10-18; stopped after 12 of 50 at the user's request).** The 12 lookups were done by NPI search in the directory's "PPO – Large Group/Small Group" network, 2026, with a name search when the NPI returned nothing:
- **3 of the 12 were found:**
    - Coast Plaza Hospital, Norwalk: **matches**.
    - Paru Chaudhari MD Inc, Walnut Creek, found through its owner-practitioner: **matches**.
    - A hospital-based OB/GYN: the directory lists Arcadia; our best address is Pasadena, **differs**.
- **9 organisation NPIs weren't listed at all**, by NPI or by name. They include White Memorial Medical Center, UC Irvine Pediatrics and a Sutter lab.

So the directory indexes clinicians and facilities rather than group or billing NPIs. And several organisations in the shared CA rate file don't appear in the PPO directory, which supports open question Q3 (the file mixes networks). Precision on 3 found rows (2 of 3) is too small a sample to quote.

The directory also confirmed independently that **EOA = "Elect Open Access", a POS product**: it's listed as "POS – Elect Open Access Small Group/Large Group". On the next run, C5 reads the file (never overwriting it) and reports **precision for verified and unverified addresses** in `c5_stats.json → directory_check`. That gives the judges a measured accuracy for the address mapping, beyond agreement between sources. If the directory shows a different site for a provider, that's direct evidence of stale data, and it belongs in the notes.

Other ways to use the directory, if the organizers allow:
- ask Health Net or the organizers for a directory extract (California requires health plans to keep directories accurate, so one exists internally);
- check whether Health Net offers a FHIR provider-directory API for this line of business. CMS requires one for Medicare Advantage and Medicaid plans, but not for commercial plans.

**What's left** (3,055 NPIs not verified; 1,225 of them corroborated):

| Remaining group | NPIs | What would confirm them |
|---|---|---|
| Corroborated (NPPES + a name + ZIP site) | 1,225 (1,212 organisations) | An identifier-linked second source for the same site: DAC by NPI, the CA register by NPI, or a directory extract |
| A single source, almost always NPPES | 1,341 (1,049 organisations) | A second independent address: the Health Net directory (spot check or extract), the CMS POS hospital/non-hospital extract, the CA Secretary of State business registry (principal address by entity name) |
| Several candidates, none confirmed | 366 | The same sources; the geocoded county can already be checked against `PPO: <County>` labels where present |
| Only out-of-state addresses (national labs, mail-order, telehealth) | 114 | Mostly correct as-is (e.g. LabCorp, Quest headquarters). Confirming a CA site needs the lab's own location list |
| Deactivated / not in NPPES | 9 | Not mappable from public registries; flag for the organizers |

Each new source becomes another `source` with its own weight and family. The rule doesn't change.

## 8. Limitations
- 49.3% of NPIs have neither a verified nor a corroborated address (see §7), and only 17.8% are verified by identifier-linked sources.
- The CLIA extract is from Q3 2023. A certificate active then may have closed since, so CLIA is weighted lower and never verifies alone.
- The IRS address is a filing address: it raises confidence and links a TIN, but it isn't treated as a practice site on its own.
- Name matching is rule-based (word overlap, place words removed, at least 2 shared words). The thresholds (0.5 for TIN names, 0.6 for site names) are configurable, and every similarity is reported.
- Census geocoding confirms that an address exists, not that the provider practises there. 342 real addresses aren't in the Census street file (No_Match) and stay unconfirmed, not rejected.
- The directory check measures accuracy on a 50-NPI sample only, and only once someone fills it in.
