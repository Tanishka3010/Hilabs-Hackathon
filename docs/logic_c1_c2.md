# Logic: C1 (index parsing) and C2 (PPO network identification)

Scope: payer Centene, commercial **group** market, California, **PPO** network. The scope is configuration, not code: `config/config.yaml` → `c2.target_states` (a list), `c2.target_market`, `c2.target_network`.
Inputs: the 11 TiC index (table-of-contents) files in `index_dir`, snapshot 2026-09-28/30.

## 1. Data sources
| brand | index files | plans | market | in-network files |
|---|---|---|---|---|
| healthnet | 1 | 1,693 | group | 2 (`healthnet-ca`, `healthnet-or`) |
| ambetter | 1 | 1,120 | individual | 28 (one per state, `ambetter-<st>`) |
| fidelis | 1 | 94 | individual | 2 (`fidelis-ex`, `fidelis-es`, NY) |
| qualchoice | 8 (base + 7 sub-indexes) | 363 unique (368 entries) | group | 1 (shared `qualchoice`, AR) |

Facts that hold across every file (from `outputs/c1/recon_report.md`):
- **Schema:** all files are v2.0.0. Every `reporting_structure` item has `reporting_plans`, `in_network_files` (with `description` and `location`) and `allowed_amount_file`.
- **Reporting entity:** always `Centene Management Company LLC` (TPA), so it can't tell brands or issuers apart.

## 2. C1: parsing rule
- **Streaming:** each index file is streamed with `ijson` (one `reporting_structure` item at a time) in a process pool.
- **Brand and sub-index:** `brand` comes from the index file name (`<date>_<brand>[_<sub_index>]_index[ (n)].json`). QualChoice sub-indexes keep `brand = qualchoice` and record the sub-index in `sub_index`.
- **Output rows:** one row per (plan entry × in-network file entry) in `plan_file_long`. A plan entry with no in-network file is kept, with null file columns, and counted in `plan_entries_without_file`.
- **Schema variants:** missing optional keys (`plan_sponsor_name`, `plan_market_type`, `description`) become null. An `in_network_files` item given as a bare string is treated as its URL. No variant is dropped.
- **Plan identity:** `plan_key` is a hash of (brand, plan_name, plan_id, plan_id_type, plan_market_type), with whitespace trimmed. Repeated plan entries collapse into one row of `plans`: 368 QualChoice entries → 363 plans. That's 4 City of Fort Smith plans listed in both `big_four` and `city_of_fort_smith`, plus 1 plan (`PreferredChoice $500 Deductible`) listed twice in the base index.

## 3. C1: dedup rule
Two in-network entries are the **same file** if and only if their normalized URLs are equal. Normalization does the following:
1. Trim whitespace.
2. Lowercase the scheme and host, and drop default ports (`:443` for https, `:80` for http).
3. Drop the whole query string and fragment. This removes SAS/CloudFront signatures, expiry and tracking parameters.
4. Canonicalize percent-encoding in the path (decode, then re-encode), so `Centene Corporate` and `Centene%20Corporate` match.
5. **Keep the path's case.** Object stores are case-sensitive, so `A.json` and `a.json` stay different files.

`file_key` is `sha1(normalized_url)[:16]`. Files that share a file name but have different normalized URLs are **reported** in `same_name_diff_url.csv` and **not merged**.

Result on the snapshot:
- 40 in-network entries → 33 unique files. The 7 removed duplicates are QualChoice sub-indexes re-listing the shared `qualchoice` file.
- 0 URLs merged by normalization alone.
- 0 file-name collisions.

## 4. C2: the key fact that shapes everything
The index has **one in-network file for all 1,312 Health Net CA group plans**: `2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json` (about 702 MB). HMO, PPO, EOA and POS plans all point to it. So:
- The **PPO network is identified at plan level.** There is no PPO-only file to pick.
- The **PPO rate file is that single CA file, labelled `MIXED`** (`requires_in_file_network_filter = True` in `ppo_files.csv`). Rates in a TiC file are not tied to plans, so the carve-out inside the file uses in-file evidence only: C4 drops provider groups whose TIN business name is labelled HMO-only and every capitation price (an HMO payment model), and keeps a `network_label` on every remaining rate. See [logic_c4.md](logic_c4.md) §1.

No machine learning is used. There are no independent labels to train on: any labels would come from the same name tokens, so a model would just re-learn the regex. Every decision below is a rule with a reason code.

## 5. Stage A: scope filters
Each plan is checked against the filters below. Every filter that fails is recorded in `scope_reasons_all`, and the first one is the primary `scope_reason`.

| order | filter | evidence (in precedence order) | reason code |
|---|---|---|---|
| 1 | state = CA | (a) state token in the in-network file URL (`_healthnet-ca_`, `_ambetter-tx_`; only valid US state codes, so `fidelis-ex/-es` give nothing); (b) state in a HIOS-shaped `plan_id` (`67138CA073` → CA); (c) the brand's state, *derived from data*, used only if ≥90% of the brand's HIOS ids share one state (Fidelis → NY 88/88, QualChoice → AR 85/85) | `OUT_OF_STATE`, `STATE_UNKNOWN` |
| 2 | `plan_market_type == group` | index field | `INDIVIDUAL_MARKET`, `MARKET_UNKNOWN`, `NON_GROUP_MARKET` |
| 3 | commercial product | word match on Medicare / Medi-Cal / Medicaid / D-SNP / CHIP | `NON_COMMERCIAL_PRODUCT` |
| 4 | medical product | name starts with Dental/Vision, or says "dental/vision only/plan" | `DENTAL_VISION_BH_ONLY` |

Results (3,270 unique plans):

| outcome | plans | who |
|---|---|---|
| `OUT_OF_STATE` | 1,900 | Ambetter in 27 non-CA states (1,062), QualChoice AR (363), Health Net OR (381), Fidelis NY (94) |
| `INDIVIDUAL_MARKET` | 58 | Ambetter CA |
| **IN_SCOPE** | **1,312** | Health Net CA group |

Filters 3 and 4 matched 0 plans; they are kept as guards. Ambetter plans named "+ Vision + Adult Dental" are medical plans with add-ons, so they don't match. MHN commercial plans don't appear separately in the index; they would show up under Health Net.

**Market and ID-type cross-check.** These are flagged in `data_flags` and `conflicts.csv`, and never used to override the market field:
- `ID_TYPE_CONFLICT_GROUP_WITH_HIOS`: 308 unique plans (the 307 QualChoice base-index plans plus Oregon `10940OR029`).
- `ID_SHAPE_CONFLICT_EIN_TYPED_HIOS_VALUE`: 154 Health Net CA plans typed `ein` whose plan_id is HIOS-shaped (`67138CA0xx`). These are in scope, so they appear in `conflicts.csv` as `DATA_FLAG`.

The rule "EIN means group, HIOS means individual" therefore does **not** hold in this data. The market is taken from `plan_market_type`.

## 6. Stage B: network-type signals
Each signal is its own column in `plan_classification.csv`.

| signal | column | how it works | result on the snapshot |
|---|---|---|---|
| Plan-name token | `name_tokens`, `name_label` | Regex `(?<![A-Za-z])(PPO\|HMO\|EPO\|POS\|HSP\|EOA)(?![A-Za-z])`, case-insensitive. Digits may touch the token (`PPO5000`); letters may not (`POST` is not POS). One type gives that label; `PPO+EPO`, `MULTI:…` and the special tokens EOA/HSP are kept as their own values. | PPO 452, HMO 719, POS 6, EOA 127, PPO+EPO 3, none 5 |
| Glued token | `glued_label` | Narrow exception: a token glued to a known prefix from `config.c2.glued_prefixes` (`CUS`). | 1: `CA L CUSPPO 15/250/20% RAP` → PPO |
| File token | `file_signal` | Same regex on the file name, description and URL path. | 0 hits: no Centene file name carries a network type |
| Legal entity | `entity_signal` | Config rules on `issuer_name`/`plan_sponsor_name` (`Life Insurance` → PPO, `Health Net of California` → HMO). | 0 hits: every Health Net plan says `Healthnet, LLC` |
| Product family | `family_hits`, `family_signal` | A candidate family name is accepted only if, among in-scope plans with an explicit token, it appears ≥5 times and always with one type (purity 1.0). | See table below |
| Propagation | `propagation_signal` | Bipartite plan↔file graph. A file is confidently type X only if every plan on it is X with high or medium confidence; a plan inherits only if all its files agree. | 0: the CA file is MIXED, so nothing propagates |
| Cost-share shape | `costshare_shape`, `costshare_signal` | Counts the parts of the cost-share string (`30/500/4000/30%` = 4). A shape is used only if it reaches ≥95% precision on ≥20 explicit-token plans. | Only the 2-part shape passes (PPO 34/35); it decides 0 plans |
| Fingerprint (`--fingerprint`) | `fp_*` in `file_classification.csv` | Streams `provider_references` of candidate files (byte cap 40 MB, group cap 200k); NPI/TIN sets, Jaccard, NPPES CA share; results cached. | CA file: `network_name` empty in 2,825/2,825 refs; `PPO:` prefix on 1.9% of groups; NPPES not provided |

**Product-family validation** (`c2_stats.json` → `family_validation`). Purity is measured against every plan whose network is explicit, including EOA plans, which count as their own network:

| family | support | types among explicit-token plans | accepted? |
|---|---|---|---|
| SC | 230 | HMO 230 | yes → HMO |
| SAL | 163 | HMO 163 | yes → HMO |
| EC | 140 | HMO 85, EOA 55 | **no**: it also appears on EOA plans, so it is a tier code, not a network marker |
| WholeCare | 34 | HMO 34 | yes → HMO |
| CalChoice | 32 | HMO 32 | yes → HMO |
| SmartCare | 30 | HMO 30 | yes → HMO |
| Full Network | 30 | HMO 30 | yes → HMO |
| Salud | 26 | HMO 26 | yes → HMO |
| CommunityCare, Trio, PureCare, EnhancedCare, Elect, Select, Open Access, Mex | 0 | not in data | no |

Two checks back this up:
- 513 of the 719 HMO-token plans also carry an accepted HMO family name.
- **None of the 456 PPO plans carries any HMO family name.** The PPO/HMO split is consistent across both signals.

## 7. Stage C: decision precedence
The same table is in code (`PRECEDENCE`, `decide()`) and in `c2_stats.json`. The first rule that fires decides.

| rank | rule | confidence | reason code |
|---|---|---|---|
| P1 | Exactly one network token in the plan name | high | `P1_PLAN_NAME_TOKEN` |
| P1b | Token glued to a known prefix (no clean token present) | medium | `P1b_PLAN_NAME_GLUED_TOKEN` |
| P1x | Special token: EOA, HSP, PPO+EPO, several types. Mapped **only** by a config switch (`eoa_as`, `hsp_as`, `ppo_epo_as`), never filled in from lower signals, because the name itself says what the product is. Confidence is high when the organizers confirmed the mapping (`organizer_confirmed`), low otherwise; with no mapping the plan stays unresolved. | high (confirmed) / low | `P1x_EOA_AS_EOA_ORGANIZER_CONFIRMED`, `P1x_PPO_EPO_AS_PPO_ORGANIZER_CONFIRMED`; unmapped: `EOA_UNRESOLVED`, `HSP_UNRESOLVED`, `PPO_EPO_CONFLICT`, `MULTI_NETWORK_TOKEN_CONFLICT` |
| P2 | All of the plan's files carry the same file-name token | high | `P2_FILE_TOKEN` |
| P3 | Legal-entity rule | medium | `P3_LEGAL_ENTITY` |
| P4 | Accepted product family | medium | `P4_PRODUCT_FAMILY` |
| P5 | File-sharing propagation | medium | `P5_FILE_PROPAGATION` |
| P6 | Accepted cost-share shape | low | `P6_COSTSHARE_SHAPE` |
| none | No signal fired | low | `NO_NETWORK_EVIDENCE` (label `OTHER/UNKNOWN`) |

The fingerprint is file-level evidence only. It never labels a plan.

**Final in-scope result (1,312 plans):**

| label | plans | distinct names | how decided |
|---|---|---|---|
| **PPO** | **456** | 136 | 452 P1 (high), 3 P1x PPO+EPO confirmed as PPO (high), 1 P1b (medium) |
| HMO | 723 | 286 | 719 P1 (high), 4 P4 via `SAL` (medium) |
| EOA | 127 | 49 | P1x, confirmed by the organizers as a separate network (high) |
| POS | 6 | 6 | P1 (high) |
| EPO | 0 | 0 | no EPO-only plan exists |
| OTHER/UNKNOWN | 0 | 0 | nothing left unresolved |

**PPO network: 456 plans.** The organizers confirmed two rules (2026-10-17): EOA is a separate network, not PPO, and plans named both PPO and EPO count as PPO.

## 8. How HMO, EPO and POS were ruled out
- **HMO, 723 plans.** 719 carry the word HMO, e.g. `CA L SC HMO 40/500d/4500`, `WholeCare HMO Gold $50 INF`, `Salud HMO y Mas Gold $30`, `CA L CUS HMO AC Transit`. Another 4 have no type word but belong to the accepted HMO family `SAL`: `CA L SAL Mex 5/0/1500` (×3) and `CA L&T CUS SAL CEMENT MASONS CA H&W GF+`. No HMO plan carries a PPO token.
- **POS, 6 plans.** All are custom employer plans with an explicit POS word: `CA L CUS POS County of Kern GF`, `… COUNTY OF SANTA CLARA`, `… City of Mountain View`, `… Oak Card Club`, `… Victor Valley USD`, `CA L CUS CBA POS CA Ironworkers`. `UC POST DOC` plans are **not** POS (tested), because `POST` fails the word boundary.
- **EPO, 0 EPO-only plans.** EPO appears only in 3 names that also say PPO: `CA L CUS PPO CA IRONWORKERS FWP EPO$15`, `… FWP-EPO$10`, `CA L CUS PPO SACRAMENTO CITY USD- EPO`. The organizers confirmed these count as PPO (`ppo_epo_as: ppo`), so they are in the PPO network.
- **EOA, 127 plans (49 names), ruled out as a separate network.** The organizers confirmed EOA is its own network, not PPO (`eoa_as: eoa`). None of these names has a PPO word. Example: `CA L EOA 20/0/1500`, `CA L EC EOA 30/20%/2500`.
- **HSP:** does not occur.

## 9. File-level classification
For each of the 33 files, `file_classification.csv` gives the label, confidence, reason, `n_ppo_plans`, `n_non_ppo_plans`, `n_unknown_plans`, the label counts, file tokens, the propagation label and any conflicts:

| file label | files | detail |
|---|---|---|
| **MIXED** (in scope) | 1 | `healthnet-ca`: PPO 456, HMO 723, EOA 127, POS 6; PPO share 34.8% |
| out of scope | 32 | 31 `OUT_OF_STATE`, 1 `INDIVIDUAL_MARKET` (`ambetter-ca`) |

`ppo_files.csv` holds that one CA file, with `requires_in_file_network_filter = True`. `ppo_plans.csv` holds the 456 PPO plan keys. The in-file filter is applied in C4 ([logic_c4.md](logic_c4.md) §1).

## 10. Conflict handling
`conflicts.csv` lists 466 rows. 156 are in scope:

| kind (in scope) | rows |
|---|---|
| `DATA_FLAG`: ein-typed plan with a HIOS-shaped id | 154 |
| `WEAK_SIGNAL_DISAGREES`: `CA L CUS EC HMO FED's 30/50` has the 2-part cost-share shape (PPO) but the HMO token wins | 1 |
| `FILE_PLANS_OF_DIFFERENT_TYPES_SHARE_FILE`: the MIXED CA file | 1 |

The remaining 310 rows are out of scope: 308 ID-type data flags (307 QualChoice, 1 Oregon) and 2 out-of-scope MIXED files (`ambetter-ca`: HMO 27 + PPO 31; `qualchoice`: HMO/PPO/POS/unknown). Out-of-scope files are still labelled from their own plans, for information only.

## 11. Hypotheses tested
| hypothesis | result |
|---|---|
| A PPO-specific in-network file exists | **Rejected.** One CA file for all networks. |
| Knox-Keene split: HMO = Health Net of California, PPO/EPO = Health Net Life | **Not testable.** `issuer_name` is `Healthnet, LLC` everywhere, and HIOS issuer `67138` covers HMO (120) and PPO (34) plans. |
| `plan_id_type` implies market (EIN = group, HIOS = individual) | **Rejected.** 308 group plans are typed HIOS. |
| "Health Net HSP" is an HMO-like product | **Not applicable.** HSP is absent. |
| Product family names imply network type | **Confirmed for 7 families** (purity 1.0, support 26 to 230). `EC` rejected: it appears on HMO and EOA plans. |
| Cost-share shape implies network type | **Mostly rejected.** 4-part shape is PPO only 87% of the time, 3-part is HMO 72%; only the 2-part shape passes (97%). |
| In-file `network_name` separates networks (TiC v2) | **Rejected on sample.** Empty in 2,825/2,825 provider references. |
| TIN `business_name` prefixes tag networks | **Weak.** `PPO:` on 1.9% of groups, plus `Direct Network: …` (including `AMBETTER PPO/HMO`). Used in C4: HMO-only labels and capitation prices are excluded; PPO labels are kept as `network_label`. |

## 12. Known limitations
- All PPO evidence comes from plan names. If the issuer mislabels a plan name, it is misclassified.
- 130 plans (EOA and PPO+EPO) are classified by the organizers' answers, not by evidence in the public data.
- The PPO rate file is shared with HMO/POS/EOA plans. The C4 carve-out removes only providers with explicit HMO evidence (HMO-only labels, capitation); providers with no label stay in, so the rate file can still contain providers that are only in other Health Net CA networks.
- The fingerprint reads only a byte-capped prefix of the 702 MB file, and the NPPES CA share needs `nppes_dir`.
- Family names were validated against this snapshot only, and could drift in later snapshots.

## 13. Reproduce and switch assumptions
```bash
.venv/bin/python -m src.run --stage all
```
To include the provider fingerprint:
```bash
.venv/bin/python -m src.run --stage c2 --fingerprint
```
To change how EOA or PPO+EPO plans are treated, edit `config/config.yaml` → `c2.eoa_as` / `c2.ppo_epo_as` / `c2.hsp_as` (`unknown|ppo|hmo|epo|eoa`) and `c2.organizer_confirmed`, then re-run. Current setting: `eoa_as: eoa`, `ppo_epo_as: ppo`, both organizer-confirmed. The chosen settings are written into `c2_stats.json → config`.

Open questions for the organizers are in [open_questions.md](open_questions.md).
