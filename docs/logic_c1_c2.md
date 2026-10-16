# Logic: C1 (index parsing) and C2 (PPO network identification)

Scope: payer Centene, commercial **group** market, California, **PPO** network.
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
- **Plan identity:** `plan_key` is a hash of (brand, plan_name, plan_id, plan_id_type, plan_market_type), with whitespace trimmed. The same plan listed in two sub-indexes collapses into one row of `plans` (4 cases: the City of Fort Smith plans in `big_four` and `city_of_fort_smith`).

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

## 4. C2: scope filters, signals, precedence
_Pending review of the recon findings; see `docs/open_questions.md`._
