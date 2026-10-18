# Stage 1 – Repo discovery (2026-10-18)

- 101 tracked files; repo dir 13 GB because of git-ignored `raw/` (NPPES 11.7 GB, PTD 702 MB, sources 246 MB).
- Language: Python 3 only. Deps pinned exactly in requirements.txt (ijson, pandas 3.0.6, pyarrow, duckdb, PyYAML, openpyxl, xlsxwriter, pytest).
- Entry point: `python -m src.run --stage {recon,c1,c2,c4,c3,c5,c6,all}` (src/run.py:8-17). `all` order: recon→c1→c2→c4→c3→c5→c6.
- Config: config/config.yaml + config/stages/{c3,c4,c5,c6}.yaml merged via setdefault (src/common.py:33-46). All paths relative to ROOT; CLI overrides.
- Tests: tests/ (10 files, ~1,000 lines).
- Docs: docs/logic_c1_c2.md, logic_c3..c6.md, open_questions.md, PROJECT_CONTEXT.md (modified, uncommitted), scalability_review.md (untracked), work_split.md, HiLabs_PTD_C1_C2_Summary.docx.
- Outputs committed: outputs/c1..c6 (except xlsx), SUMMARY.md, summary.json.

## External data sources
| Source | Where | Obtained by | Contributes |
|---|---|---|---|
| 11 Centene index JSON | DATA/ (committed) | manual | C1/C2 |
| Health Net CA in-network JSON (702 MB) | raw/ptd/ | auto download (ptd_reader.download) | C3/C4 |
| NPPES npidata + pl_pfile | raw/nppes/ | MANUAL (undocumented URL in README) | C3 validity, C5 candidates, specialty |
| NUCC taxonomy CSV | reference/ (committed) | manual | C6 specialty name |
| HiLabs benchmark xlsx | reference/ (committed) | manual | C3 coverage weights, taxonomy xwalk |
| CMS DAC national CSV | reference/ (git-ignored); dac_ca.parquet committed fallback | MANUAL | C5 dac, dac_group |
| CDPH health_facility_locations.csv | raw/sources (git-ignored) | MANUAL | C5 ca_facility (high trust) |
| IRS eo_ca.csv | raw/sources | MANUAL | C5 irs_eo |
| CMS POS CLIA Q3 2023 | raw/sources | MANUAL | C5 cms_clia |
| Census batch geocoder | API | auto, cached outputs/_cache/geocode | C5 canonical address, county |

=> Clean-checkout reproduction requires 5 manual downloads; README does not give URLs for NPPES/DAC/CDPH/IRS/CLIA (only says files are needed). logic_c5.md names the sources but not exact URLs either.
