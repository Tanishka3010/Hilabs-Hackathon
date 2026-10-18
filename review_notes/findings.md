# Findings register

| ID | Sev | Category | Where | Finding |
|---|---|---|---|---|
| F-001 | Critical | Correctness | c4_extract_rates.py:8-9; c3_quality.py:128-131; ptd_reader.py:163-165 | RC prices repeat one rate across many item names (99.2% of RC rows), and additional_information is usually not the item's procedure (2.4% name agreement; RC/CPT median ratio 49×). `name` in the context key and the derived `procedure_code` inflate rows and mislabel prices (e.g. "36415 venipuncture" at $7,990). |
| F-002 | High | Logic | c4_extract_rates.py:36-37 | No in-file PPO filter: HMO/EOA/POS providers in the shared file are output as PPO. Capitation (an HMO marker, 72 NPIs) and `DIRECT NETWORK: AMBETTER HMO` labels are captured but not used. |
| F-003 | High | Correctness | c6_rate_file.py:13-19 | Deliverable grain is not unique: 98.8% of rows share (NPI,TIN,Address,Billing Code); 52 groups look identical in Excel but have different rates (setting/arrangement hidden). |
| F-004 | High | Coverage | c5_provider_location.py:70-85, 234-236 | 63% of "verified" NPIs = NPPES + a name+ZIP match searched only in NPPES ZIPs, so the two sources are not fully independent. 43% of best addresses are NPPES-only. |
| F-005 | Medium | Logic | c5_provider_location.py:299-309, 429-432 | TIN→address is the union of NPI addresses; "nppes_org_name_matches_tin" counts as TIN-specific though it says nothing about the address. TIN "verified" = any NPI under it verified. |
| F-006 | Medium | Correctness | reference_data.py:54-55; c6_rate_file.py:55-69 | Specialty taken from Taxonomy slot 1, not the primary-switch taxonomy: 607 NPIs (16%) affected. |
| F-007 | Medium | Metrics | c6_rate_file.py:219-224 | 97% "rows with verified address" is row-weighted; 10 NPIs = 46% of rows. NPI share is 50.8%. |
| F-008 | Medium | Scalability | ptd_reader.py:200-213; c4_extract_rates.py:128-134 | No .json.gz, no remote provider_references, files extracted serially, 3 full passes per file. |
| F-009 | Medium | Robustness | ptd_reader.py:46-66, 177 | Download has no retry/checksum; if the server ignores Range, the resume appends a full body. An inline non-numeric NPI raises and kills the stage. |
| F-010 | Medium | Reproducibility | README.md; c5_provider_location.py:424-428 | 5 manual downloads with no URLs or fetch script; missing sources are skipped with a warning (silently changes results); the manual directory check exists only in committed outputs. |
| F-011 | Low | Reproducibility | c4_extract_rates.py:61, 99 | string_agg order and row order are not deterministic (rates identical). |
| F-012 | Low | Reproducibility | outputs/c2/ppo_files.csv | The committed file has fp_* columns, which `--stage all` does not produce. |
| F-013 | Medium | Scalability | c4_extract_rates.py:94; c6_rate_file.py:209, 116-139 | Peaks of 4.2 GB (C4) and 3.3 GB (C6) for a 702 MB file; no DuckDB memory_limit/temp_directory; Excel written with a per-cell Python loop (60 s). |
| F-014 | Low | Scalability | c2_classify_network.py:13-15; config.yaml:23-25 | Market/network hardcoded; entity rules and C5 sources are CA/Health Net-specific. |
| F-015 | Low | Scalability | c5_provider_location.py:205-230, 290-309 | Python row loops in C5 scoring and linking. |
| F-016 | Low | Robustness | geocode.py:41, 72-77 | Missing results are cached as No_Response forever; one failed batch aborts C5. |
| F-017 | Low | Docs | README.md; logic_c1_c2.md §4 vs logic_c4.md §1 | Docs say C3 filters PPO rates, but C4 keeps all. The README stage table and Run section omit or misorder c5/c6. |
| F-018 | Low | Metrics | c3_quality.py:51-52 vs c4_extract_rates.py:8-9 | Conflict context differs between C3 (4,194) and C4 (4,859). |
| F-019 | Low | Security | c4:99, c6:113, c6:210 | SQL built with unescaped path strings. |
| F-020 | Medium | Metrics | c3_quality.py:202-214 | Duplicate and conflict metrics don't detect name-level replication (F-001), so exact_duplicates = 0.85% understates redundancy; code_identifiability 0.75 is overstated. |
| F-021 | High | Deliverables | repo | No video, no C7, no UI in the repo. |
| F-022 | Low | Correctness | c4 output | Capitation and FFS rates for one service are both kept as separate "PPO" rows. |
