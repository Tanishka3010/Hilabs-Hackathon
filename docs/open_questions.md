# Open questions for the organizers

Each item below is something the data leaves ambiguous. Until it's answered, the pipeline does **not** guess. The "Current handling" line on each item says what the code does in the meantime. Evidence comes from `outputs/c1/recon_report.md` (index snapshot 2026-09-28/30).

## Q1. What network is Health Net `EOA`?
- **Evidence:** 127 CA group plan entries (49 distinct names) carry `EOA` and no PPO or HMO token. Examples: `CA L EOA 20/0/1500`, `CA L EC EOA 30/20%/2500`, `CA L CUS EOA City of La Verne GF`.
- **Why it matters:** if EOA is a PPO-type (open-access) product, the PPO plan count rises from 453 to 580 (583 if the 3 PPO+EPO plans are PPO too).
- **Hint in the data:** 55 of the 127 EOA entries also carry the `EC` prefix (e.g. `CA L EC EOA 30/20%/2500`). Elsewhere `EC` appears only on HMO plans (85 of 85), which suggests EOA may be HMO-based. This is recorded as a conflict, not used to decide.
- **Current handling:** labelled `OTHER/UNKNOWN`, reason code `EOA_UNRESOLVED`, excluded from PPO and listed in `conflicts.csv`. Switch: `c2.eoa_as: unknown|ppo|hmo`.

## Q2. Plans named both PPO and EPO
- **Evidence:** `CA L CUS PPO CA IRONWORKERS FWP EPO$15`, `CA L CUS PPO CA IRONWORKERS FWP-EPO$10`, `CA L CUS PPO SACRAMENTO CITY USD- EPO`.
- **Question:** is this a PPO plan with an EPO option, or an EPO plan running on the PPO network?
- **Current handling:** `OTHER/UNKNOWN`, reason code `PPO_EPO_CONFLICT`, not counted as PPO. Switch: `c2.ppo_epo_as: conflict|ppo|epo`.

## Q3. A single CA in-network file for every CA group network
- **Evidence:** all 1,312 Health Net CA plan entries (HMO 719, PPO 452, EOA 127, POS 6, other 8) point to `2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json` (about 702 MB).
- **Question:** is a "PPO network file" expected to exist, or should the PPO network be carved out of this file (C3) using plan-level membership and/or in-file provider attributes?
- **Current handling:** the file is labelled `MIXED` and is the only row in `outputs/c2/ppo_files.csv` (`requires_in_file_network_filter = True`), with the 453 PPO plans in `outputs/c2/ppo_plans.csv`.

## Q4. What do the in-file network markers mean?
- **Evidence:** in the first 40 MB of the CA file, `provider_references[].network_name` is always empty (2,825 of 2,825). About 2% of TIN `business_name` values carry prefixes such as `PPO: Los Angeles County`, `Direct Network: San Diego`, `DIRECT NETWORK: AMBETTER PPO` and `DIRECT NETWORK: AMBETTER HMO`.
- **Question:** are these prefixes reliable network tags?
- **Note:** `AMBETTER` appearing inside a commercial-group file is unexpected.

## Q5. Health Net plan-name abbreviations
- **Abbreviations to confirm:** `CA L` / `CA T` / `CA L&T` (large group? trust?), `SC`, `EC`, `SAL` (Salud?), `CUS` (custom), `CBA`, `GF`, `INF`, `Aon NCA/SCA`, `Mex`.
- **Plans with no network token:** `CA L SAL Mex 5/0/1500` and `CA L&T CUS SAL CEMENT MASONS CA H&W GF+`. Is `SAL` always the Salud HMO? In the data, `SAL` appears with HMO in 163 of 163 explicit-token plans, so C2 labels these 4 entries HMO (`P4_PRODUCT_FAMILY`, medium confidence).

## Q6. Glued token `CUSPPO`
- **Evidence:** `CA L CUSPPO 15/250/20% RAP` looks like `CUS PPO` with the space missing. A strict word-boundary regex misses it.
- **Current handling:** a narrow exception for the known `CUS` prefix (`c2.glued_prefixes`), giving PPO with medium confidence (`P1b_PLAN_NAME_GLUED_TOKEN`).

## Q7. plan_id_type conflicts
- **Evidence:**
  - 154 Health Net CA plans are typed `ein` but their plan_id is HIOS-shaped (`67138CA0xx`).
  - All 307 unique QualChoice base-index plans are typed `hios` with market `group`.
  - 1 Oregon plan is typed `hios` with market `group` (`10940OR029`).
- **Current handling:** flagged, not used to override `plan_market_type`.

## Q8. Legal-entity evidence
- **Evidence:** `issuer_name` is `Healthnet, LLC` for all 1,693 Health Net plans. HIOS issuer prefix `67138` covers both HMO (120) and PPO (34) plans.
- **Question:** is there an authoritative mapping from plan to licensed entity (Health Net of California, Inc. vs Health Net Life Insurance Company)? Without one, the Knox-Keene hypothesis can't be tested.

## Q9. Duplicate QualChoice sub-indexes
- **Evidence:** `big_four` and `city_of_fort_smith` contain the same 4 City of Fort Smith plans. They are deduplicated at plan level. Out of scope anyway (Arkansas).
