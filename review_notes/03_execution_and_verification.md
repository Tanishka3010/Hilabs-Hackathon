# Stage 3 – What was run and what it showed

## Runs (macOS, Python 3.14.6, ijson backend yajl2_c)
- `pytest -q`: 143 passed, 4.1 s.
- `python -m src.run --stage all --output-dir <scratch> --probe-mb 0`, with `outputs/_cache` copied in (so NPPES 11.7 GB scan and Census geocoding were served from cache; not re-tested cold). Exit 0, 92 s wall, peak RSS 4.1 GB.
- Per stage (scratch, warm caches): c4 3.1 s / 4.17 GB peak (extract reused; fresh extract ≈ 8 s / 430 MB per raw/extract_log.txt and my run), c3 1.0 s / 1.24 GB, c5 15.9 s / 0.49 GB, c6 60.5 s / 3.34 GB.
- Rerun vs committed outputs: identical for c1, c2 plans, c3 scores, c5 address/link CSVs, c6 parquet (DataFrame.equals True). Differences: rates_selected row order + `provider_group_ids` string_agg order (264,958 rows; rates identical by set-compare), metric_details top-N tie order, ppo_files/file_classification (committed copy has fp_* fingerprint columns from `--fingerprint`), directory_check_sample regenerated blank in fresh dir.

## Independent rate re-derivation (scratchpad/verify_rates.py, no project imports)
20 stratified rows (6 Type 1, 6 Type 2 with conflicts, 4 RC+procedure code, 4 multi-address pairs), re-parsed from raw JSON with ijson and the documented rule → **20/20 match**.

## Plan→file chain: 5 random PPO plans
All 5 found in DATA/2026-09-30_healthnet_index.json reporting_structure 0 → healthnet-ca_in-network.json → file_key 5bf4afa2cef9bc2a → all 1,466,674 C6 rows carry that key. Trivial chain: one file for 1,312 plans.

## Grain checks on outputs/c6/provider_rate_file.parquet (npi_valid rows = 1,466,669)
- Unique on full context (npi,tin,address_id,arrangement,code type,code,procedure,name,modifier,class,setting,POS): 0 dups.
- At deliverable grain (NPI,TIN,Address,Billing Code): 12,759 keys hold 1,449,547 rows (98.8% of rows non-unique).
- On the 16 visible Excel columns: 339 duplicate groups (678 rows), 52 with different rates (e.g. NPI 1023125465 CPT 90837: capitation $233.08 vs ffs $240.25 — arrangement not shown).
- address '' 46 rows, specialty '' 49 rows, rate null/≤0: 0.
- Addresses per (NPI,TIN): 1 → 3,521 pairs; 2–15 → 252 pairs; max 47 verified addresses. Pairs with >1 address are all verified (tiebreak = keep all verified).
- Row concentration: top 10 NPIs = 669,402 rows (46%); pairs with ≥5 addresses = 400,620 rows. Type 1 NPIs = 1,051 rows only.

## RC replication finding
- rates.parquet: 1,484,488 of 1,496,307 RC price rows (99.2%) sit in (provider group, RC, additional_information, POS) cells where one identical rate repeats across several item names.
- RC rows whose additional_information is a CPT/HCPCS code present in the file: 410,724; item name equals that code's name in 9,732 (2.4%).
- Raw example: item "ASSAY OF SEX HORMONE GLOBUL" (RC 300) carries additional_information "36415" (venipuncture); 70 RC-300 items share the same rate set per provider.
- RC-derived "procedure" median vs CPT/HCPCS median on 201 shared codes: median ratio 49×; 157 codes >10× (36415: $7,990 vs $9.34; 80053: $8,938 vs $10.56).

## In-file network signals
- negotiation_arrangement = capitation: 46 prices / 19 provider groups → 558 C6 rows, 72 NPIs, 34 TINs (kept as PPO).
- network_hint on output: PPO 186 rows/99 NPIs; DIRECT NETWORK 93 rows/46 NPIs.

## C5 dependency
- Best address rests only on NPPES family: 1,603 of 3,727 NPIs (43%).
- Verified 1,899: 1,887 two-family, 12 CA register; 1,200 (63%) are NPPES + only name+ZIP-derived sources (dac_group/cms_clia/ca_facility_name_zip) whose ZIP search is restricted to the NPI's NPPES ZIPs.
- NPPES slot-1 taxonomy not primary: 607 of 3,733 NPIs (16%).

## Address precision spot check (15 links, web search 2026-10-18)
| # | Tier | NPI / name | Address | Evidence used | Judgment |
|---|---|---|---|---|---|
| 1 | verified ≥0.85 | 1528066685 BHC Sierra Vista Hospital | 8001 Bruceville Rd, Sacramento | ca_facility+cms_clia+nppes | Plausible (Kaiser directory, HCAI) |
| 2 | verified ≥0.85 | 1750398863 OC Retina Med Grp | 999 N Tustin Ave Ste 17, Santa Ana | cms_clia+dac_group+nppes | Plausible (MemorialCare, CalOptima); 2nd site 1200 N Tustin not output |
| 3 | verified ≥0.85 | 1407116361 Monterey Peninsula Surgery Center | 2265 41st Ave, Capitola | ca_facility (by NPI)+nppes | Plausible (WebMD: Capitola Surgery Center at that address) |
| 4 | verified <0.85 | 1811902786 Norman Lavin | 18370 Burbank Blvd Ste 204, Tarzana | dac+nppes | Plausible (Optum, MediFind) |
| 5 | verified <0.85 | 1861054264 Sebastien Montel | 27201 Puerta Real Ste 300, Mission Viejo | dac+nppes | Unconfirmed (no web hit) |
| 6 | verified <0.85 | 1275621054 Stuart Katzman | 1624 E Capitol Expy, San Jose | dac+nppes | Plausible (ChoosePT, Healthline) |
| 7 | verified <0.85 | 1164615001 Rhonda Beckwith | 777 E Alosta Ave, Azusa | irs_eo (TIN = church)+nppes | Plausible city (Vitadox Azusa); address is the TIN's church filing address |
| 8 | unverified single | 1578928297 Nikki Thibault | 1693 Mission Dr #204, Solvang | nppes | Plausible (counseling practice at address) |
| 9 | unverified single | 1619197381 Allergy & Asthmatic Disease MG | 39000 Bob Hope Dr, Rancho Mirage | nppes | Plausible (Cortico) |
| 10 | unverified single | 1477303824 H Dermatology PC | 11900 South St, Cerritos | nppes | Unconfirmed |
| 11 | unverified multi | 1912436205 Taylor Tica | 8880 Rio San Diego Dr Ste 800 | nppes | Doubtful (listed online-only, Stanton area) |
| 12 | unverified multi | 1407864374 Lucile Packard Children's | 725 Welch Rd, Palo Alto | irs_eo | Plausible — correct (Stanford Children's pages) |
| 13 | unverified multi | 1538259437 Steven Schenkel | 566 S San Vicente Blvd, LA | dac | Unconfirmed |
| 14 | out of state | 1104829159 LabCorp | 1236 Huffman Mill Rd, Burlington NC | cms_clia | Real LabCorp site (NCDOT list), not a CA practice site |
| 15 | out of state | 1902376924 LabCorp | 7444 W Alaska Dr, Lakewood CO | cms_clia+nppes | Real lab processing site, not CA |
Estimated precision: verified 6/7 plausible (86%), unverified 3/6 (50%), with 1 doubtful and 2 unconfirmed; n is tiny.
