# Stage 2 – Module-by-module notes (all src/ files read in full)

| Module | Lines | Notes |
|---|---|---|
| run.py | 66 | Stage dispatcher; `all` = recon,c1,c2,c4,c3,c5,c6. Summary printed only for c2/all. |
| common.py | 126 | ROOT-relative paths (no hardcoded abs paths). normalize_url drops query+fragment (L60-70). ijson streaming of reporting_structure (L81-83). |
| contracts.py | 76 | Column/dtype contracts; validate_header used by C3. |
| c0_recon.py | 364 | Schema discovery from ijson events; optional 40 MB HTTP Range probe. Report only. |
| c1_parse_index.py | 184 | Process pool over index files; plan×file long table; plan_key/file_key = sha1. Correct, tested. |
| c2_classify_network.py | 456 | Scope filters (state from URL/HIOS/brand, market, non-commercial, dental) → precedence P1..P6 with reason codes; families validated by purity; conflicts.csv. TARGET_MARKET/NETWORK hardcoded (L13-15). |
| c2_fingerprint.py | 109 | Optional; byte-capped provider_refs probe, Jaccard, NPPES CA share; cached. File-level evidence only. |
| ptd_reader.py | 230 | Resumable download (no retry); 3 sequential ijson passes (header, provider_references, in_network); Parquet batches 250k. Plain JSON only (no gzip). Inline NPI `int(npi)` unguarded (L177). Item `description` not extracted (L163-165). |
| c4_extract_rates.py | 143 | DuckDB selection SQL: drop invalid/expired/non-dollar → best type → 10× outliers set aside → mode → lower median. Context includes `name` (L8-9). No in-file network filter (joins all refs, L36-37). |
| c3_quality.py | 403 | 13 rate-file metrics + 5 index metrics, share/tolerance/freshness scoring, weighted mean, grades. procedure_code view derives CPT from additional_information (L128-131). as_of = max input date (deterministic). |
| reference_data.py | 323 | Chunked NPPES/PL/DAC streaming, NPI-scoped caches; NPI Luhn check (L71-82). Uses `Healthcare Provider Taxonomy Code_1` not the primary-switch slot (L54-55). |
| geocode.py | 80 | Census batch API, threaded, 3 retries, parquet cache; missing → cached "No_Response" forever (L72-77). |
| c5_provider_location.py | 464 | 8 candidate sources, address key (street+ZIP5), Census canonicalization with house-number/ZIP safety, noisy-OR confidence, verified = ≥2 families or CA register, Python groupby loops (L205-230, L290-309). TIN→address = union of NPI addresses re-ranked by TIN name evidence. |
| c6_rate_file.py | 243 | DuckDB join rates × pair addresses × NPPES attrs → Parquet; xlsxwriter constant_memory Python loop (60 s). Excel lacks setting / negotiation_arrangement columns. |

Tests: 143 pass in 4 s (synthetic fixtures for every stage; c4 selection rules; c5 scoring; c6 sheet split). No test for gzip, inline-NPI errors, RC name replication, or end-to-end on real data.
Security: no secrets; SQL strings built with f-strings around file paths (c4:99, c6:113, c6:210) — only local paths, low risk. NPPES/DAC data is public; no PHI.
