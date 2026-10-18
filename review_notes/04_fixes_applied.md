# Fixes applied (2026-10-18), in order of criticality

All outputs were regenerated with `python -m src.run --stage all` (143 s, peak 2.7 GB, was 4.1 GB). Reruns are byte-identical for rates_selected, the C6 Parquet, npi_addresses, metric_details and the C7 output. Tests: 157 pass (was 143).

| ID | Fix | Where | Effect on this run |
|---|---|---|---|
| F-001 | Rates repeated across item names are collapsed per cell (provider group, code, additional_information, class, setting, POS, modifier, arrangement, rate type). The name stays in the key only when rates differ by name. `additional_information` becomes `associated_code`; `procedure_code` is set only when the code identifies the item. Item `description` is extracted | c3_quality.connect_rates; contracts.CONTEXT_COLS; ptd_reader | 1,500,288 rows → 458,759 prices; selected rates 840,574 → 271,053 |
| F-020 | New C3 metric `rate_replication`; `code_identifiability` uses validated procedure codes; listing metrics read published rows, price metrics read collapsed prices | c3_quality.rate_metrics | Replication 69% (score 0); identifiability 0.75 → 0.015; grade B (0.857) → C (0.765) |
| F-002, F-022 | In-file PPO carve-out: capitation prices and HMO-only labelled provider groups are dropped (configurable), listed in `non_ppo_exclusions.csv`; `network_label` on every row | c4_extract_rates; config/stages/c4.yaml | 34 groups / 91 NPIs / 49 TINs excluded from some rates; 19 pairs leave entirely |
| F-003 | Excel gets Associated Code, Setting, Arrangement and Network Label columns; C6 fails if two rows share the full grain; About sheet explains the grain | c6_rate_file (EXCEL_COLUMNS, GRAIN, grain_check) | 0 duplicate keys; 457,783 rows, 1 sheet, 37 MB |
| F-004 | Verification split into `verified` (two identifier-linked families, or CA register) and `corroborated` (NPPES + a name+ZIP site) | c5_provider_location.score_addresses; c5.yaml name_linked_sources | NPIs verified 17.8%, verified or corroborated 50.7% (was "50.8% verified") |
| F-005 | TIN-specific evidence = IRS EIN address, DAC group name, or TIN consensus (another NPI under the TIN at the same address). An NPPES org-name match no longer counts. TIN stats split three ways | c5.link_tin_npi_address, run | TIN-specific pairs 673 / 3,761 (18%, was "41%"); TINs with TIN-specific verified address 4.5% |
| F-006 | Primary taxonomy (switch = Y across 15 slots) used for specialty; NPPES cache versioned (v2) | reference_data.primary_taxonomy | 607 NPIs get a different specialty; specialty coverage 0.493 → 0.500 |
| F-007 | SUMMARY.md leads with NPI and TIN shares; the row-weighted share is labelled as such | c6.summary_metrics, write_summary_md | |
| F-008 | gzip input (magic-byte detection); remote `provider_references` fetched and cached; files extracted in parallel; the two passes per file run in parallel; C3 grades files in parallel | ptd_reader, c4.run, c3.run | Extraction 9.7 s → 7 s |
| F-009 | Download retries with backoff, resume trusted only on HTTP 206, restart from 0 otherwise; non-numeric inline NPI counted, not fatal | ptd_reader.download, to_npi | |
| F-010 | Missing C5 sources stop the run unless `c5.allow_missing_sources: true` (then listed in `missing_sources`). The directory check moved to `reference/` so a fresh output folder never loses it. Source URLs were **not** added (user's instruction) | c5.load_source; c5.yaml | |
| F-011 | `string_agg(... ORDER BY)`, total ORDER BY keys, deterministic tie-breaks in metric details | c4, c3 | Byte-identical reruns |
| F-012 | Committed outputs regenerated with the documented command (no `--fingerprint`) | outputs/ | |
| F-013 | DuckDB memory cap (`duckdb.memory_limit`, default 2 GB) and spill directory; Excel written column-batch-wise | common.duck; c6.write_excel | C6 60 s → 20 s; C4 peak 4.2 → 2.1 GB |
| F-014 | Scope in config: `c2.target_states` (list), `target_market`, `target_network` | c2_classify_network.scope | |
| F-015 | C5 scoring and TIN linking rewritten with grouped aggregations and merges | c5 | |
| F-016 | Geocoder misses are no longer cached; a failed batch no longer aborts C5 | geocode.py | |
| F-017 | Docs aligned: PPO carve-out happens in C4. README covers every stage; Q3 updated, Q10 added | README, docs/* | |
| F-018 | One context definition shared by C3 and C4 (`contracts.CONTEXT_COLS`) | contracts | C3 1,694 vs C4 1,681 (C4 is after the HMO-evidence carve-out) |
| F-019 | SQL paths and literals quoted via `sql_str` / `quote_path` | c3, c4, c6, common | |
| F-021 | C7 cost effectiveness added; UI added (`outputs/ui/index.html`). Video not done (user's instruction) | c7_cost_effectiveness.py, ui_report.py, docs/logic_c7.md | 685 providers labelled: 190 cost effective, 330 in line, 165 high cost |

Also: the reference caches are now built once per run for the rate file's full NPI set (C3, C5 and C6 share them).
