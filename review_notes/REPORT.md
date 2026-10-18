# Review: Centene CA PPO PTD pipeline (2026-10-18)

Sources for every claim: `review_notes/01_discovery.md`, `02_code_review.md`, `03_execution_and_verification.md`, `findings.md`. Every file under `src/`, `tests/`, `config/` and `docs/*.md` was read. Not read: `docs/HiLabs_PTD_C1_C2_Summary.docx`, `docs/work_split.md` (internal), and the `recon_report.md` body.

## 1. Executive summary
**Verdict:** a well-engineered pipeline with an unusually thorough audit trail. Its rates re-derive exactly (20/20). Two problems with how the data is read undermine the main deliverable.

**Strengths**
1. C2 is a rule cascade with reason codes and purity-validated product families. All 1,312 in-scope plans are resolved.
2. C3 has 13 justified, weighted file metrics plus a grade for each index file.
3. Lineage runs end to end: selection_rule, rate_min/max, sources, verification_basis, link_evidence.

**Risks**
1. **Revenue-code replication (F-001).** 99% of RC prices repeat one rate across many service names. The derived "procedure code" is mostly not the service, so rows are inflated about 40× and mislabelled.
2. **No PPO carve-out inside the shared file (F-002).**
3. **Rate file not unique at the required grain (F-003).** There is also no video, C7 or UI (F-021).

**Readiness:** about 70%. It is submittable after the must-fix items in §10, about one to two days of work.

## 2. Repo overview
- **Layout:** `src/` (14 modules, about 3,500 lines), `tests/` (143 tests), `config/` (YAML, stage files merged), `docs/` (logic files per stage), `outputs/` (committed results except the xlsx), `DATA/` (11 index files), `raw/` (git-ignored: NPPES 11.7 GB, PTD 702 MB, CDPH/IRS/CLIA).
- **Run:** `python -m src.run --stage all`, order recon → c1 → c2 → c4 → c3 → c5 → c6.
- **What I ran:**
  - `pytest`: 143 passed in 4 s.
  - Full pipeline into a scratch output directory with the existing reference and geocode caches: exit 0, 92 s, peak 4.1 GB.
  - Outputs match the committed ones, except non-deterministic ID-string order and the fingerprint columns (F-011, F-012).
  - Not exercised cold: the NPPES scan and live Census geocoding (both cache hits).
- **External sources:**

| Source | Feeds | How it is obtained |
|---|---|---|
| Index files | C1/C2 | committed |
| Health Net CA in-network file | C3/C4 | downloaded automatically |
| NPPES npidata + pl_pfile | C3, C5, specialty | manual |
| NUCC taxonomy | specialty names | committed |
| HiLabs benchmark xlsx | C3 weights, taxonomy crosswalk | committed |
| CMS DAC | C5 | manual (CA parquet committed as fallback) |
| CDPH facility register | C5 | manual |
| IRS EO (CA) | C5 | manual |
| CMS POS CLIA 2023 | C5 | manual |
| Census geocoder | C5 | API |

## 3. Code review findings
| ID | Sev | Category | File:line | Issue | Suggested fix |
|---|---|---|---|---|---|
| F-001 | Critical | Correctness | c4_extract_rates.py:8-9; c3_quality.py:128-131; ptd_reader.py:163-165 | 1,484,488 of 1,496,307 RC rows repeat one rate across item names. `additional_information` matches the item's service in only 2.4% of cases (e.g. "SHBG assay" carries 36415). RC-derived medians are 49× the CPT medians (36415: $7,990 vs $9.34). The context includes `name`, which multiplies rows | Collapse RC rows to (provider, RC, additional_information, POS) and keep names as a list. Rename `procedure_code` to `associated_code` for RC rows. Extract item `description`. Add a C3 "rate replication" metric |
| F-002 | High | Logic | c4_extract_rates.py:36-37 | All providers in the MIXED file are emitted as PPO. Capitation rows (an HMO marker: 46 prices, 72 NPIs) and `AMBETTER HMO` labels are kept | Flag or exclude capitation rows and HMO-labelled groups, carry `network_hint` into the Excel, and state the residual risk in numbers |
| F-003 | High | Correctness | c6_rate_file.py:13-19 | 12,759 (NPI,TIN,Address,Code) keys cover 98.8% of rows. 52 groups are identical on every visible column but have different rates | Add Setting and Arrangement columns to the Excel, or collapse to the grain by a stated rule. Document the extended grain on the About sheet |
| F-004 | High | Coverage | c5_provider_location.py:70-85, 234-236 | 1,200 of 1,899 verifications pair NPPES with a name+ZIP match that was searched in NPPES ZIPs | Report these as "corroborated", or search by city/county independently of the NPPES ZIP. Publish the strict share |
| F-021 | High | Deliverables | repo | No video, C7 or UI | Record the video (mandatory) |
| F-005 | Medium | Logic | c5:299-309, 429-432 | TIN–address evidence is mostly NPPES org-name ↔ TIN-name. A TIN counts as verified if any NPI under it is verified | Report only IRS/DAC-group matches as TIN-specific, and define TIN verification separately |
| F-006 | Medium | Correctness | reference_data.py:54-55 | Specialty comes from taxonomy slot 1, not the primary one (607 NPIs, 16%) | Read all 15 taxonomy slots and pick the one with the primary switch = Y |
| F-007 | Medium | Metrics | c6_rate_file.py:219-224 | 97% "rows verified" is driven by large hospital systems (10 NPIs = 46% of rows) | Headline the NPI share (50.8%) and TIN share (49.7%) |
| F-008 | Medium | Scalability | ptd_reader.py:200-213; c4:128-134 | No gzip, no remote provider_references, serial extraction, 3 passes per file | Wrap reads in gzip, use a process pool per file, fetch provider_references by URL |
| F-009 | Medium | Robustness | ptd_reader.py:46-66, 177 | Download has no retry or checksum, and the resume path assumes Range support. `int(npi)` on inline groups is unguarded | Retry with backoff, check for a 206 response, try/except plus an invalid_records counter |
| F-010 | Medium | Reproducibility | README; c5:424-428 | 5 manual downloads without URLs. A missing source only warns. The manual check lives only in outputs | Add a `fetch_sources` script with URLs and checksums. Fail loudly unless `--allow-missing` is passed. Move the check CSV to `reference/` |
| F-013 | Medium | Scalability | c4:94; c6:209, 116-139 | 4.2 GB / 3.3 GB peaks; Excel written with a per-cell Python loop | Set DuckDB `memory_limit` and `temp_directory`; write the Excel per column batch |
| F-020 | Medium | Metrics | c3_quality.py:202-214 | Duplicate and identifiability metrics don't see the replication in F-001 | Compute duplicates excluding `name`; identifiability only where the code matches the item |
| F-011 | Low | Reproducibility | c4:61, 99 | `string_agg` order and row order change between runs | `string_agg(... ORDER BY ...)` and a total ORDER BY |
| F-012 | Low | Reproducibility | outputs/c2 | Committed outputs include `--fingerprint` columns | Regenerate, or document the flag |
| F-014 | Low | Scalability | c2:13-15 | Market and network are hardcoded | Move them to config |
| F-015 | Low | Scalability | c5:205-230, 290-309 | Python groupby loops | Set-based DuckDB |
| F-016 | Low | Robustness | geocode.py:41, 72-77 | No_Response is cached forever; a single failed batch aborts C5 | Don't cache misses; degrade per batch |
| F-017 | Low | Docs | README; logic_c1_c2 §4 | Says C3 filters PPO rates; C4 does not | Align the docs |
| F-018 | Low | Metrics | c3:51-52 vs c4:8-9 | Two different conflict definitions (4,194 vs 4,859) | One shared context definition |
| F-019 | Low | Security | c4:99; c6:113, 210 | Paths interpolated into SQL unescaped | Use quote_path everywhere |
| F-022 | Low | Correctness | C4 output | Capitation and FFS rates for one service are both kept | Prefer FFS, or flag them (with F-002) |

## 4. Challenge audit
| C | What / where | Assessment | Score |
|---|---|---|---|
| C1 | c1_parse_index.py; common.normalize_url | Streams, runs in parallel, handles schema variants. 40 → 33 files, 3,275 → 3,270 plans; dedup rule stated and tested | 9/10 |
| C2 | c2_classify_network.py:231-248 | Explicit precedence. HMO is ruled out by token plus validated families (no PPO plan carries an HMO family name). EPO-only = 0; POS = 6 by token; EOA by organizer answer. Ambiguous plans are handled by config, not silently. A false-positive/false-negative estimate is only an upper bound (456 = 456). Weakness: everything rests on plan names, and the file level can't isolate PPO | 8/10 |
| C3 | c3_quality.py | 13 metrics with definitions, reasons, tolerances and weights, computed from data; every used file (1 rate file plus 11 index files) is scored. Gaps: missed RC replication (F-020); metrics describe the mixed file, not PPO | 7/10 |
| C4 | c4_extract_rates.py:18-89 | Rule stated, applied consistently, tested; 20/20 re-derivations match. But the context key (`name`, `additional_information` as procedure) is wrong for 99% of rows, and there is no PPO carve-out | 5/10 |
| C5 | c5_provider_location.py | Goes well beyond the NPPES baseline (below). Verified: 50.8% of NPIs, 49.7% of TINs. Independence is weaker than claimed (F-004), TIN linkage is thin (F-005), and only 12 of 50 directory checks are done | 7/10 |
| C6 | c6_rate_file.py | Excel and Parquet are produced, splitting at the 1.05M-row limit, with full evidence columns. Not unique at the required grain, specialty uses taxonomy slot 1, and the 97% headline is misleading | 6/10 |
| C7 | — | Absent | 0/10 |

**C5 baseline test.** Techniques beyond "standardise NPPES and score it":

| Technique | Where |
|---|---|
| CA CDPH register by NPI (high trust) and by name+ZIP | c5:136-144 |
| CMS DAC by NPI | c5:184-185 |
| DAC group practice name+ZIP | c5:147-155 |
| CMS CLIA name+ZIP | c5:158-164 |
| IRS EIN = TIN | c5:167-172 |
| NPPES recency decay | c5:126-133 |
| Census geocoding with a safety rule | c5:97-123 |
| County label cross-check | c5:313-321 |
| Directory sample | c5:328-361 |

There are no claims-like signals and no TIN-level consensus across NPIs.

**How much rests on NPPES alone:** 43% of NPIs (1,603) have a best address supported only by NPPES. Another 1,200 verifications pair NPPES with a source that was found by searching NPPES ZIPs.

**Address precision (15 links, web check):**

| Tier | Plausible | Notes |
|---|---|---|
| Verified, confidence ≥ 0.85 | 3/3 | |
| Verified, confidence < 0.85 | 3/4 | 1 unconfirmed |
| Unverified | 3/6 | 1 doubtful (therapist listed as online-only), 2 unconfirmed |
| Out-of-state | 2/2 | Real LabCorp sites, not CA practice locations |

Estimated precision is about 86% for verified links and about 50% for unverified links. The sample is too small to quote. Detail is in `03_execution_and_verification.md`.

## 5. Deliverables checklist
| # | Deliverable | Status | Notes |
|---|---|---|---|
| 1 | Rate Excel | Partial | 1,466,669 rows, 103 MB, rebuilt by `--stage c6` (not committed). Grain and RC issues (F-001, F-003) |
| 2 | Logic file | Present | logic_c1_c2.md and logic_c5.md cover sources, rules and conflicts. Inconsistent on PPO filtering (F-017) |
| 3 | Quality metrics | Present | logic_c3.md and outputs/c3 |
| 4 | Summary | Present | outputs/SUMMARY.md; every number re-checked against the stats JSONs and my rerun (1899/3735 = 0.5084, 1435/2888 = 0.4969, 840,574 selected rates, etc.). The row-weighted 97% is misleading (F-007) |
| 5 | Video | Missing | |
| 6 | Repo | Partial | Re-runs cleanly, no hardcoded paths, pinned dependencies. Needs 5 manual downloads without URLs (F-010) |
| 7 | Brownie points | Missing | No UI, no C7 |

## 6. Product comparison
| Product | Feature | Status | Evidence | Gap or advantage | Stage |
|---|---|---|---|---|---|
| A | 1 Validation and verification | Adequate | reference_data.py:71-82 (NPI check digit); c3:262-290 (NPPES exists, active, in state); c2:151-159 (ID-shape flags) | No TIN format check; no source cross-check of the identifier itself | C2/C3/C5 |
| A | 2 Field standardization | Partial | c5:29-44 (address key); c5:103-123 (Census canonical form); c6:55-69 (taxonomy → NUCC/HiLabs) | Rule-based, not AI. TIN only has dashes stripped. Wrong taxonomy slot (F-006) | C5/C6 |
| A | 3 Business rules | Adequate | c2:231-248 plus config switches; c4:30-33 (placeholder, expiry, rate type) | Effective dates limited to expiration_date; no plan-specific rules | C2/C4 |
| A | 4 Any-format ingestion | Partial | common.py:97-107 (string/dict file entries, v1/v2); CSV/xlsx loaders | No gzip, no remote provider_references, no roster formats (F-008) | C1/C4 |
| A | 5 Reconciliation | Partial | c2:269-295 (conflicts.csv); c4:79-86 (rate conflicts); c5 multi-source agreement | No comparison against a prior run or existing records. Real-time is out of scope | C2/C4/C5 |
| A | 6 Audit trail | Strong | decision_reason / final_reason_code (c2:380-384); selection_rule (c4:81-85); invalid_records (ptd_reader:131-181); verification_basis, link_evidence; invalid-NPI rows kept (c6:214) | No persisted run log or override register | C2–C6 |
| A | 7 Downstream integration | Out of scope | file outputs only | A production integration concern | no mapping |
| A | 8 Zero-touch automation | Partial | run.py one command | Manual source downloads and a manual directory check | all |
| A | 9 Turnaround | Strong | 92 s warm rerun; 702 MB extracted in about 8 s | Far under 48 h, but at single-file scale | all |
| B | 1 Multi-source validation | Adequate | c5:136-196 (8 sources); c5:234-236 (two-family rule) | Dependence issue (F-004). No claims, EHR or web sources (claims out of scope) | C5 |
| B | 2 R3 confidence | Partial | c5:203-216 (noisy-OR reliability); c5:126-133 (recency, NPPES only) | No recency for CDPH data_date or the 2023 CLIA file; relevance only as link_evidence; no separate R/R/R scores | C5 |
| B | 3 Geospatial validation | Adequate | c5:97-123 (geocode safety rule); c5:313-321 (county check) | No distance plausibility check; ZIP/city consistency not checked beyond the geocoder | C5 |
| B | 4 Ingestion and standardization | Adequate | c5:54-67 (name normalization); reference_data loaders | Rule-based | C5 |
| B | 5 Coverage breadth | Partial | addresses, lat/long, county, taxonomy | Phone is loaded (reference_data DAC/NPPES rename) but never output; no hours or languages | C5/C6 |
| B | 6 Business rules and validation | Adequate | in-state requirement, deactivated status, CA register high trust (c5:227-236, 253-265) | No plan or regulatory directory rules | C5 |
| B | 7 Continuous monitoring | Absent | — | No re-run diff or move detection. A batch diff would be in scope | C5 |
| B | 8 Feedback and anomaly analysis | Partial | c5:328-361 (stratified directory sample and precision scorer); C3 outlier metric | 12 of 50 checks done; no pattern analysis of failures | C3/C5 |

## 7. Limitations vs the products (ranked)
1. **Unreliable source interpretation, which a validation product would catch** (F-001). Maps to A1/A2. Hits the correctness answer key and the Logic score. Effort M.
2. **Weak independence of verification** (F-004/F-005). Maps to B1/B2. Coverage (35 points) depends on the "verified" definition holding up. Effort M.
3. **No PPO membership validation for providers** (F-002). Maps to A3/A5. This is the core of Logic C2/C4. Effort S (flags) to M.
4. **No recency for non-NPPES sources and no R3-style split.** Maps to B2. Effort S.
5. **Ingestion limited to plain JSON** (F-008). Maps to A4. Scalability is worth 40 points. Effort S–M.
6. **No re-run diffing or monitoring.** Maps to B7/A5. Effort M; nice to have.
7. **Directory feedback loop incomplete** (12/50). Maps to B8. Measured precision helps Coverage. Effort S.

## 8. Strengths vs the products
1. **File-level PTD grading** (c3_quality.py): 13 metrics, tolerance scoring and grades, plus index-file grading. Neither product description covers grading payer files.
2. **PPO inference from plan metadata** (c2_classify_network.py): family purity validation, glued-token handling, organizer-confirmed switches, conflicts register. Not described for either product.
3. **Audit trail per decision** (matches A6): every plan, rate and address carries reason codes and evidence columns.
4. **Deterministic, explained rate selection with full lineage** (c4): rule order, set-aside outliers and the rate range are kept; independently re-derived 20/20.
5. **Geocoding safety rule** (c5:97-101): rejects approximate matches that change the house number or ZIP. A concrete improvement on naive geospatial validation (B3), in a narrow way.

No genuine strength was found for A7, A8, B5 or B7.

## 9. Rubric projection
**Logic track: about 70/100**

| Criterion | Projected | Reasoning |
|---|---|---|
| Logic | 28/40 | C2 is excellent. C4/C6 RC handling and the missing PPO carve-out cost points |
| Metrics | 26/35 | Well defined and justified; misses replication; mixed-file caveat |
| Structure | 16/25 | Strong logic docs and summary; no video; small doc inconsistencies |

**Scale track: about 64/100**

| Criterion | Projected | Reasoning |
|---|---|---|
| Scalability | 24/40 | Streaming everywhere and DuckDB out-of-core. Parallel only for index files and geocoding; no gzip; scaling never demonstrated on a second file |
| Coverage | 23/35 | Every NPI has a candidate address. 50.8% of NPIs and 49.7% of TINs verified, with independence caveats |
| Correctness | 17/25 | Rates match the stated rule and reruns are clean. But a key that treats RC rows and PPO membership differently would likely mismatch, and reproduction needs manual data |

## 10. Prioritized action plan
**Must fix before submission**

| # | Fix | Finding | Criterion | Effort |
|---|---|---|---|---|
| 1 | Collapse RC replication, rename `procedure_code` → `associated_code` for RC rows, extract `description`, rerun C3–C6, update docs | F-001, F-020 | Correctness, Logic, Metrics | M |
| 2 | Add Setting and Arrangement columns to the Excel, document the grain, flag capitation | F-003, F-022 | Correctness, Structure | S |
| 3 | Flag or exclude non-PPO evidence (capitation, `AMBETTER HMO` labels), show `network_hint` in the Excel, quantify the residual mixed-network risk | F-002 | Logic | S |
| 4 | Record the ≤5-minute video | F-021 | Structure | M |
| 5 | Use the primary taxonomy for specialty | F-006 | Logic, Coverage | S |
| 6 | Headline NPI and TIN verified shares; report strict vs corroborated verification; fix the TIN-specific definition | F-004, F-005, F-007 | Coverage, Metrics | S |
| 7 | Add source URLs and a fetch script to the README; fail loudly on missing sources | F-010 | Correctness/reproducibility | S |

**Nice to have**

| # | Fix | Finding | Criterion | Effort |
|---|---|---|---|---|
| 8 | gzip streaming, process-pool extraction, proof run on `healthnet-or` / `ambetter-ca` with timings | F-008 | Scalability | S–M |
| 9 | Finish the 38 remaining directory checks and report measured precision | — | Coverage | S |
| 10 | DuckDB memory_limit, deterministic `string_agg`, download retries, guarded inline NPI | F-009, F-011, F-013 | Scalability, Robustness | S |

Also nice to have: C7 peer-group metrics, a UI, C5 rewritten set-based (F-015).

## 11. Unknowns
- **Answer key:** which grain and rules it uses (RC rows, PPO filter). Needs organizer guidance.
- **Cold run:** the NPPES 11.7 GB scan and live Census geocoding weren't re-executed (caches used). Needs a run with an empty `outputs/_cache`.
- **Clean clone:** not tested from a fresh clone with only the README; the manual sources were already present.
- **Excel:** not opened in Excel; the xlsx was written successfully (102.9 MB).
- **Spot-check gaps:** 4 of the 15 links (Montel, H Dermatology, Schenkel, and the Tica address) couldn't be confirmed by web search. Needs the CA licence lookup or a call.
- **Meaning of `additional_information` on RC rows:** needs Centene or organizer confirmation. The data strongly suggests it is not the item's procedure code.
