# C5: mapping PTD TINs and NPIs to their practising address

**Status: first version, using the sources available today.**

Every NPI in the PPO rate file is placed:
- **3,726 of 3,735 have a candidate address**, and **3,611 have one in CA**;
- **216 NPIs (5.8%) have a verified address**, meaning two independent sources agree;
- 211 of 2,888 TINs (7.3%) have at least one verified address.

All verified NPIs are individual clinicians. **Organisations (3,054 NPIs, 82% of the file) can't be verified yet**, because NPPES is their only address source. The facility registries listed in §7 are the next step and plug into the same scoring.

Reproduce:

```bash
.venv/bin/python -m src.run --stage c5
```

It runs in about 7 s once the reference caches exist.

## 1. The relationship being built

> **TIN → NPI** comes from the rate file: each provider group lists NPIs under one TIN. **NPI → address** comes from external sources. **TIN → address** is the union of its NPIs' addresses, ranked per pair.

The brief warns that one TIN can have many addresses and one NPI can practise at many. So nothing is collapsed early: every (NPI, address) candidate is kept and scored, and every (TIN, NPI) pair gets its own ranked address list.

| Item | Count |
|---|---|
| NPIs in the PPO rate file | 3,735 (672 individuals, 3,054 organisations, 2 not in NPPES) |
| TINs | 2,888 |
| NPI–TIN pairs | 3,780 |

## 2. Address sources used now

| Source | What it contributes | Trust (weight) | Why |
|---|---|---|---|
| CMS DAC (national file) | Practice addresses of Medicare-enrolled clinicians, plus the **group practice name** per address | 0.7 | Refreshed from Medicare enrollment (PECOS), so more current than NPPES. Clinicians only |
| NPPES primary practice address | One address for every registered NPI | 0.5 × recency | Universal but self-reported. The brief says about 80% are stale |
| NPPES secondary practice locations (`pl_pfile`) | Extra sites for 467 NPIs | 0.4 × recency | Same registry, so less independent |
| The rate file (TIN business names) | Links NPI → TIN, and organisation names to check against | (link evidence) | It's the payer's own current contract data |

**Recency** adjusts NPPES trust using `Last Update Date`, relative to the as-of date (2026-10-06):

| Last updated | Factor |
|---|---|
| Within 1 year | 1.0 |
| 1 to 3 years ago | 0.7 |
| More than 3 years ago | 0.4 |

61% of these NPIs haven't been updated since 2023, which is exactly why NPPES alone isn't trusted.

Candidates found: 3,726 NPPES primary, 1,663 secondary and 498 DAC addresses. They reduce to 5,504 distinct NPI–address pairs.

## 3. Matching the same address written differently

The sources spell addresses differently (e.g. `22 CORPORATE PLAZA #150` vs `22 Corporate Plz`), so each one is normalised into an **address key**:

1. Join the two address lines and upper-case them.
2. Strip suite, unit, building and floor parts (`STE 320`, `#150`, `BLDG 2`). These are kept separately as `units`, so two suites in the same building count as one practice site.
3. Remove punctuation, and standardise street words and directions (STREET→ST, BOULEVARD→BLVD, NORTH→N, …).
4. Key = normalised street + ZIP5.

## 4. Scoring and verification

For each (NPI, address key):
- **Confidence** = 1 − Π(1 − weight) over the sources that report this address. Each source counts once. Agreement raises confidence; for example, DAC + a fresh NPPES primary gives 1 − 0.3 × 0.5 = 0.85.
- **Verified** = the address is reported by at least **2 independent source families** (NPPES counts as one family, DAC as another) **and** it's in CA. A single source is never "verified", however recent.
- **Best address per NPI**, in order: in CA → verified → highest confidence → most recent NPPES update → address key (a deterministic tie-break).

Each NPI gets a `location_status`:

| Status | Meaning | Individuals | Organisations | Total |
|---|---|---|---|---|
| `verified` | Best address confirmed by 2 independent sources | 216 | 0 | 216 |
| `unverified_single_source` | One in-CA candidate, one source | 305 | 2,606 | 2,911 |
| `unverified_multiple_candidates` | Several in-CA candidates, none confirmed | 144 | 340 | 484 |
| `out_of_state_only` | Every candidate is outside CA (e.g. national labs: OR 20, WA 11, NV 8, TX 8, FL 7, NY 6) | 7 | 108 | 115 |
| `deactivated_npi` | NPPES deactivated; NPPES withholds their address | 7 | 0 | 7 |
| `no_address` | Not in NPPES, and no other source | — | — | 2 |

Of the 331 clinicians DAC knows, **216 (65%) have a DAC address that matches their NPPES address**. The other 115 disagree, which is the brief's staleness warning showing up in our data.

## 5. Linking each TIN–NPI pair to an address

For each pair (`tin_npi_address.csv`), the NPI's addresses are re-ranked so **addresses tied to that TIN come first**. Evidence is recorded in `link_evidence`:

- `dac_group_name_matches_tin`: DAC lists this address under a group practice whose name matches the TIN's business name. Matching uses word overlap of at least 0.5, after removing legal suffixes and generic words (INC, LLC, MEDICAL, GROUP…) and labels like `PPO:`. This is the strongest TIN-specific evidence: it says the clinician practises *for this TIN* at *this address*. 11 address rows across 9 pairs, 6 of them also verified.
- `nppes_org_name_matches_tin`: the organisation's NPPES legal name matches the TIN's business name. This confirms the NPI → TIN link for organisations. NPPES hides the EIN (`<UNAVAIL>`), so names are the only bridge. 1,808 address rows across 1,325 pairs.
- `verified_address`: the address itself is verified.
- `npi_address_only`: no TIN-specific evidence; the NPI's own ranking applies.

1,334 of 3,780 pairs (35%) have TIN-specific evidence from either name match.

## 6. Outputs (`outputs/c5/`)

| File | One row per |
|---|---|
| `address_candidates.parquet` | Raw candidate (NPI × source × address), normalised key, unit, source date |
| `npi_addresses.csv` | NPI × distinct address: sources, confidence, verified, rank, DAC group names, NPPES status |
| `provider_locations.csv` | NPI: best address, status, confidence, counts, taxonomy |
| `tin_npi_address.csv` | TIN × NPI × address: rank for the pair, link evidence, name similarities |
| `c5_stats.json` | Coverage figures (NPIs and TINs with any, CA, and verified addresses) and the config used |

## 7. Next sources (they plug into the same scoring)

| Source | Adds | How it links |
|---|---|---|
| CMS Provider of Services file | Physical addresses of Medicare-certified facilities (hospitals, surgery centers, home health, dialysis, labs) | Name + ZIP against the NPPES and rate-file names |
| CA HCAI licensed facility list | State-verified facility addresses (high trust) | Name + address |
| IRS Exempt Organizations file (CA) | Address per EIN for nonprofit organisations | **By TIN directly** |
| US Census Geocoder (batch API) | Standardised address, existence check, county | Every candidate. The county can then be checked against the rate file's `PPO: <County>` labels |
| Health Net provider directory | Network-specific practising locations | Needs access and terms review |

Each new source becomes another `source` with its own weight and family. "Verified" then becomes reachable for organisations without changing the rule.

## 8. Limitations
- Organisations have only NPPES today, so 0% of them are verified (see §7).
- Name matching is token overlap, not a trained matcher. The threshold (0.5) is configurable, and the similarity is reported per row.
- Address keys ignore suites by design. Two suites in one building are one site, and the suites are listed in `units`.
- No geocoding yet: an address hasn't been confirmed to exist. Agreement between sources is the evidence used.
