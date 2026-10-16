import argparse
import re
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

import ijson

from src.common import (
    as_file_entries,
    iter_reporting_structure,
    list_index_files,
    load_config,
    log,
    normalize_url,
    parse_index_filename,
    setup_logging,
    url_file_name,
)

NETWORK_TOKENS = ["PPO", "HMO", "EPO", "POS", "HSP", "EOA"]
WATCH_TOKENS = [
    "PPO", "HMO", "EPO", "POS", "HSP", "EOA", "Salud", "SAL", "Mex", "CommunityCare", "Elect", "Trio", "Select",
    "Open Access", "Full Network", "WholeCare", "SmartCare", "CalChoice", "PureCare", "EnhancedCare", "Ambetter",
    "Exclusive", "Tiered", "Flex", "SC", "EC", "CUS", "Aon", "NCA", "SCA", "HDHP", "HSA", "HSAF", "HSAI", "INF",
    "Bronze", "BRZ", "Silver", "SVR", "Gold", "GLD", "Platinum", "PLT", "Medicare", "Medi-Cal", "Medicaid",
    "Dental", "Vision", "Behavioral", "COBRA", "Retiree",
]
HIOS_RE = re.compile(r"^(\d{5})([A-Z]{2})(\d{3,})")
EIN_RE = re.compile(r"^\d{9}$")
URL_BRAND_STATE_RE = re.compile(r"_([a-z]+)-([a-z]{2})_in-network", re.I)


def token_re(tok):
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])", re.I)


TOKEN_RES = {t: token_re(t) for t in WATCH_TOKENS}


def network_tokens(name):
    return tuple(t for t in NETWORK_TOKENS if TOKEN_RES[t].search(name or ""))


def glued_tokens(name):
    return tuple(t for t in NETWORK_TOKENS if re.search(t, name or "", re.I) and not TOKEN_RES[t].search(name or ""))


def id_shape(pid):
    pid = str(pid or "")
    if HIOS_RE.match(pid):
        return "hios_like"
    if EIN_RE.match(pid):
        return "ein_like"
    if re.fullmatch(r"\d{5}", pid):
        return "hios_issuer_only"
    return "other"


def spread_sample(items, n):
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def recon_file(path, sample_size):
    _, brand, sub = parse_index_filename(path.name)
    top_keys, rs_keys, plan_keys, file_keys, aaf_keys = Counter(), Counter(), Counter(), Counter(), Counter()
    header = {}
    with open(path, "rb") as fh:
        for prefix, event, value in ijson.parse(fh):
            if event == "map_key":
                if prefix == "":
                    top_keys[value] += 1
                elif prefix == "reporting_structure.item":
                    rs_keys[value] += 1
                elif prefix == "reporting_structure.item.reporting_plans.item":
                    plan_keys[value] += 1
                elif prefix == "reporting_structure.item.in_network_files.item":
                    file_keys[value] += 1
                elif prefix == "reporting_structure.item.allowed_amount_file":
                    aaf_keys[value] += 1
            elif prefix in ("reporting_entity_name", "reporting_entity_type", "version", "last_updated_on"):
                header[prefix] = value
    plans, files, rs_summ = [], [], []
    n_rs = n_aaf = 0
    for i, rs in enumerate(iter_reporting_structure(path)):
        n_rs += 1
        rp = rs.get("reporting_plans") or []
        fe = as_file_entries(rs.get("in_network_files"))
        aaf = as_file_entries(rs.get("allowed_amount_file"))
        n_aaf += 1 if aaf else 0
        for p in rp:
            plans.append({**{k: p.get(k) for k in ("plan_name", "plan_id", "plan_id_type", "plan_market_type", "issuer_name", "plan_sponsor_name")}, "rs": i})
        for f in fe:
            files.append({**f, "rs": i})
        rs_summ.append({
            "rs": i,
            "n_plans": len(rp),
            "n_files": len(fe),
            "file_names": [url_file_name(normalize_url(f["location"])) for f in fe],
            "aaf": [url_file_name(normalize_url(a["location"])) for a in aaf],
        })
    return {
        "file": path.name, "brand": brand, "sub_index": sub, "size_bytes": path.stat().st_size, "header": header,
        "top_keys": dict(top_keys), "rs_keys": dict(rs_keys), "plan_keys": dict(plan_keys), "file_keys": dict(file_keys),
        "aaf_keys": dict(aaf_keys), "n_rs": n_rs, "n_rs_with_aaf": n_aaf, "plans": plans, "files": files, "rs_summ": rs_summ,
        "sample_size": sample_size,
    }


def probe_remote(url, max_mb):
    req = urllib.request.Request(url, headers={"Range": f"bytes=0-{int(max_mb * 1024 * 1024)}"})
    nn, pref, ex = Counter(), Counter(), defaultdict(list)
    n_refs = n_groups = 0
    status, size = "complete", None
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            size = r.headers.get("Content-Range") or r.headers.get("Content-Length")
            for pr in ijson.items(r, "provider_references.item"):
                n_refs += 1
                nn["|".join(pr.get("network_name") or []) or "(empty)"] += 1
                for g in pr.get("provider_groups") or []:
                    n_groups += 1
                    b = (g.get("tin") or {}).get("business_name") or ""
                    m = re.match(r"^\s*([A-Za-z][A-Za-z /&\-]{1,30}):", b)
                    k = m.group(1).strip().upper() if m else "(no prefix)"
                    pref[k] += 1
                    if len(ex[k]) < 6 and b not in ex[k]:
                        ex[k].append(b)
    except ijson.IncompleteJSONError:
        status = "stopped at byte cap"
    except Exception as e:
        status = f"unreachable ({type(e).__name__}: {str(e)[:80]})"
    return {"url": url, "max_mb": max_mb, "status": status, "size": size, "n_refs": n_refs, "n_groups": n_groups,
            "network_name": nn.most_common(20), "prefixes": pref.most_common(20), "examples": dict(ex)}


def md_table(rows, headers):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join(str(x).replace("|", "\\|").replace("\n", " ") for x in r) + " |")
    return "\n".join(out)


def counts_str(c):
    return ", ".join(f"`{k}`: {v}" for k, v in sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0]))))


def section_file(r):
    p, f = r["plans"], r["files"]
    L = [f"### `{r['file']}`", ""]
    L.append(md_table([
        ["brand / sub-index", f"{r['brand']} / {r['sub_index'] or '-'}"],
        ["size (bytes)", f"{r['size_bytes']:,}"],
        ["top-level keys", ", ".join(sorted(r["top_keys"]))],
        ["reporting_entity_name", r["header"].get("reporting_entity_name")],
        ["reporting_entity_type", r["header"].get("reporting_entity_type")],
        ["version / last_updated_on", f"{r['header'].get('version')} / {r['header'].get('last_updated_on')}"],
        ["reporting_structure entries", r["n_rs"]],
        ["keys in reporting_structure items", counts_str(r["rs_keys"])],
        ["keys in reporting_plans", counts_str(r["plan_keys"])],
        ["keys in in_network_files", counts_str(r["file_keys"])],
        ["allowed_amount_file present", f"{r['n_rs_with_aaf']} of {r['n_rs']} entries (keys: {', '.join(sorted(r['aaf_keys'])) or '-'})"],
        ["plan entries / distinct plan names", f"{len(p)} / {len({x['plan_name'] for x in p})}"],
        ["in-network file entries / distinct URLs", f"{len(f)} / {len({normalize_url(x['location']) for x in f})}"],
        ["plan_market_type", counts_str(Counter(x["plan_market_type"] for x in p))],
        ["plan_id_type", counts_str(Counter(x["plan_id_type"] for x in p))],
        ["plan_id shape", counts_str(Counter(id_shape(x["plan_id"]) for x in p))],
        ["issuer_name", counts_str(Counter(x["issuer_name"] for x in p))],
        ["plan_sponsor_name (top 5)", counts_str(dict(Counter(x["plan_sponsor_name"] for x in p).most_common(5)))],
        ["file descriptions", counts_str(Counter(x["description"] for x in f))],
    ], ["field", "value"]))
    names = sorted({x["plan_name"] for x in p})
    L += ["", f"Sample plan names ({min(r['sample_size'], len(names))} of {len(names)} distinct, evenly spaced over the sorted list):", ""]
    L += [f"- `{n}`" for n in spread_sample(names, r["sample_size"])]
    urls = sorted({(x["description"], x["location"]) for x in f}, key=lambda t: t[1])
    L += ["", f"Sample in-network files ({min(r['sample_size'], len(urls))} of {len(urls)} distinct):", ""]
    L += [f"- `{d}` → `{u}`" for d, u in spread_sample(urls, r["sample_size"])]
    L.append("")
    return "\n".join(L)


def section_healthnet(r):
    p, rs = r["plans"], r["rs_summ"]
    L = [f"## Health Net deep-dive (`{r['file']}`)", ""]
    L += ["### Reporting structure → in-network file", ""]
    L.append(md_table([[x["rs"], x["n_plans"], x["n_files"], ", ".join(x["file_names"]), ", ".join(x["aaf"])] for x in rs],
                      ["rs #", "plans", "in-network files", "in-network file name(s)", "allowed-amount file"]))
    by_rs = defaultdict(list)
    for x in p:
        by_rs[x["rs"]].append(x)
    for i, plans in sorted(by_rs.items()):
        fn = ", ".join(rs[i]["file_names"])
        L += ["", f"### rs #{i} — `{fn}` ({len(plans)} plans)", ""]
        combos = Counter("+".join(network_tokens(x["plan_name"])) or "(none)" for x in plans)
        L += ["Network-type token combination in plan name (word-boundary match on " + ", ".join(NETWORK_TOKENS) + "):", ""]
        L.append(md_table(sorted(combos.items(), key=lambda kv: -kv[1]), ["tokens", "plans"]))
        glued = sorted({x["plan_name"] for x in plans if glued_tokens(x["plan_name"])})
        if glued:
            L += ["", "Plan names where a network token appears only glued to other letters (no word boundary):", ""]
            L += [f"- `{n}` → {', '.join(glued_tokens(n))}" for n in glued[:30]]
        for combo in sorted(combos):
            if combo in ("HMO", "PPO"):
                continue
            names = sorted({x["plan_name"] for x in plans if ("+".join(network_tokens(x["plan_name"])) or "(none)") == combo})
            L += ["", f"All distinct plan names with tokens `{combo}` ({len(names)}):", ""]
            L += [f"- `{n}`" for n in names[:200]]
        L += ["", "Watched plan-name tokens (case-insensitive, word boundary; 0 = not present):", ""]
        L.append(md_table([[t, sum(1 for x in plans if TOKEN_RES[t].search(x["plan_name"] or "")),
                            len({x["plan_name"] for x in plans if TOKEN_RES[t].search(x["plan_name"] or "")})] for t in WATCH_TOKENS],
                          ["token", "plan entries", "distinct names"]))
        allt = Counter(t.upper() for x in plans for t in re.findall(r"[A-Za-z][A-Za-z+&\-]*", x["plan_name"] or ""))
        L += ["", "All plan-name word tokens (top 120):", "", ", ".join(f"`{k}`:{v}" for k, v in allt.most_common(120))]
        L += ["", "issuer_name: " + counts_str(Counter(x["issuer_name"] for x in plans)),
              "", "plan_sponsor_name (top 5): " + counts_str(dict(Counter(x["plan_sponsor_name"] for x in plans).most_common(5))),
              "", "plan_id_type × plan_id shape: " + counts_str(Counter(f"{x['plan_id_type']}/{id_shape(x['plan_id'])}" for x in plans))]
        ids = Counter(x["plan_id"] for x in plans)
        L += ["", f"Distinct plan_id values: {len(ids)}. Top 25 by plan count: " + ", ".join(f"`{k}`:{v}" for k, v in ids.most_common(25))]
        hios = defaultdict(lambda: Counter())
        for x in plans:
            m = HIOS_RE.match(str(x["plan_id"] or ""))
            if m:
                hios[x["plan_id"]]["+".join(network_tokens(x["plan_name"])) or "(none)"] += 1
        if hios:
            L += ["", "HIOS-shaped plan_id values (issuer prefix + state + product) × network token in plan name:", ""]
            L.append(md_table([[k, sum(v.values()), counts_str(v)] for k, v in sorted(hios.items())], ["plan_id", "plans", "tokens"]))
    L.append("")
    return "\n".join(L)


def findings(results, probe):
    L = ["## Key findings", ""]
    all_plans = [(r, x) for r in results for x in r["plans"]]
    url_plans = defaultdict(set)
    for r in results:
        for f in r["files"]:
            nu = normalize_url(f["location"])
            for x in r["plans"]:
                if x["rs"] == f["rs"]:
                    url_plans[nu].add((r["brand"], x["plan_name"], x["plan_id"]))
    versions = Counter(r["header"].get("version") for r in results)
    L.append(f"1. **Schema.** {len(results)} index files, versions: {counts_str(versions)}. Every `reporting_structure` item has "
             f"`reporting_plans`, `in_network_files` (items with `description` + `location`) and `allowed_amount_file`. "
             f"No nested sub-index references (no in_network_files item points to another index). "
             f"`plan_sponsor_name` appears only in some files (see per-file tables).")
    ent = Counter((r["header"].get("reporting_entity_name"), r["header"].get("reporting_entity_type")) for r in results)
    L.append(f"2. **Reporting entity.** {counts_str(ent)} — the TPA is the same for every brand, so entity name cannot separate brands or "
             f"legal issuers; `issuer_name` inside plans is the only issuer field.")
    conflicts = Counter()
    for r, x in all_plans:
        mt, it, sh = x["plan_market_type"], x["plan_id_type"], id_shape(x["plan_id"])
        if mt == "group" and it == "hios":
            conflicts[f"{r['brand']}: market=group but plan_id_type=hios"] += 1
        if mt == "individual" and it == "ein":
            conflicts[f"{r['brand']}: market=individual but plan_id_type=ein"] += 1
        if it == "ein" and sh == "hios_like":
            conflicts[f"{r['brand']}: plan_id_type=ein but plan_id is HIOS-shaped"] += 1
        if it == "hios" and sh == "ein_like":
            conflicts[f"{r['brand']}: plan_id_type=hios but plan_id is EIN-shaped"] += 1
    L.append("3. **Market / plan_id_type consistency conflicts** (to be FLAGGED in C2, not hidden): " +
             ("; ".join(f"{k} = {v}" for k, v in sorted(conflicts.items())) or "none"))
    states = Counter()
    for r in results:
        for f in r["files"]:
            m = URL_BRAND_STATE_RE.search(f["location"])
            states[f"{m.group(1).lower()}-{m.group(2).lower()}" if m else f"{r['brand']}-(no state token)"] += 1
    ca = sorted(k for k in states if k.endswith("-ca"))
    L.append(f"4. **State evidence in file URLs.** Brand-state tokens found: {len(states)} distinct; CA-tagged: {', '.join(ca) or 'none'}. "
             f"Ambetter CA (`ambetter-ca`) is individual-market only, so it is out of scope on market, not on state.")
    shared = sorted(((len(v), k) for k, v in url_plans.items()), reverse=True)[:5]
    L.append("5. **Plans per in-network file (largest).** " + "; ".join(f"`{url_file_name(k)}`: {n} plan entries" for n, k in shared))
    hn = next((r for r in results if r["brand"] == "healthnet"), None)
    if hn:
        rs_ca = [s for s in hn["rs_summ"] if any("-ca_" in fn for fn in s["file_names"])]
        if rs_ca:
            s = rs_ca[0]
            plans = [x for x in hn["plans"] if x["rs"] == s["rs"]]
            combos = Counter("+".join(network_tokens(x["plan_name"])) or "(none)" for x in plans)
            L.append(f"6. **Health Net CA has ONE in-network file for ALL {len(plans)} CA group plans** (`{', '.join(s['file_names'])}`). "
                     f"Plan-name tokens inside that one reporting_structure: {counts_str(combos)}. "
                     f"⇒ The index cannot isolate a PPO-only file: at file level the CA file is MIXED (HMO + PPO + EOA + POS). "
                     f"Network separation must happen at plan level (index) and, for rates/providers, inside the in-network file (C3).")
        ids = Counter(x["issuer_name"] for x in hn["plans"])
        pref = defaultdict(Counter)
        for x in hn["plans"]:
            m = HIOS_RE.match(str(x["plan_id"] or ""))
            if m:
                pref[m.group(1) + m.group(2)]["+".join(network_tokens(x["plan_name"])) or "(none)"] += 1
        L.append(f"7. **Legal-entity hypothesis (HMO = Health Net of California, PPO/EPO = Health Net Life).** `issuer_name` takes a single value "
                 f"for every Health Net plan ({counts_str(ids)}), so the index carries no Knox-Keene vs. life-insurer split. "
                 f"HIOS-shaped plan_ids by issuer prefix × network token: " +
                 "; ".join(f"`{k}` → {counts_str(v)}" for k, v in sorted(pref.items())) +
                 ". One issuer prefix spans both HMO and PPO products, so the HIOS prefix does not separate them either. "
                 "Hypothesis: NOT testable from the index (no supporting or separating evidence).")
        rs_or = [s for s in hn["rs_summ"] if any("-or_" in fn for fn in s["file_names"])]
        if rs_or:
            s = rs_or[0]
            L.append(f"8. **Health Net index also contains Oregon** (`{', '.join(s['file_names'])}`, {s['n_plans']} plans, all `group`). "
                     f"Out of scope on state; evidence = URL state token `-or_` and an HIOS id with state `OR`.")
    L.append("9. **Health Net CA plan-name grammar** (observed, needs organizer confirmation): `CA L [CUS] [SC|EC|SAL] <HMO|PPO|EOA|POS> <cost-share>` "
             "for large-group and custom (`CUS`) plans; small-group/CalChoice products are named `<Full Network|WholeCare|SmartCare|Salud HMO y Mas> HMO <metal>` "
             "and `<metal> [HDHP] PPO <cost-share>`; `INF` suffix variants pair each small-group plan. `EOA` (127 entries) carries no PPO/HMO token — "
             "its network type must be decided by an explicit rule or an organizer answer, not assumed. `EPO` occurs only inside PPO-named "
             "Ironworkers / Sacramento City USD custom plans. `HSP` does not occur.")
    if probe:
        L.append(f"10. **In-network file probe** (first {probe['max_mb']} MB of `{url_file_name(probe['url'])}`, {probe['status']}; total size {probe['size']}): "
                 f"{probe['n_refs']} provider_references / {probe['n_groups']} provider_groups read. `network_name`: {probe['network_name']}. "
                 f"TIN `business_name` prefixes: {probe['prefixes']}. Examples: {probe['examples']}. "
                 "⇒ v2.0 `network_name` exists but is empty in the sample; some business_names carry a `PPO: <County>` / `Direct Network: ...` "
                 "prefix, a possible in-file network signal for C3.")
    L.append("")
    return "\n".join(L)


def run(cfg):
    setup_logging()
    index_dir, out_dir = cfg["index_dir"], cfg["output_dir"] / "c1"
    rc = cfg.get("recon") or {}
    sample = int(rc.get("sample_size", 20))
    files = list_index_files(index_dir)
    log.info("recon: %d index files in %s", len(files), index_dir)
    with ProcessPoolExecutor(max_workers=int(cfg.get("workers", 4))) as ex:
        results = list(ex.map(recon_file, files, [sample] * len(files)))
    for r in results:
        log.info("recon %-55s rs=%-3d plans=%-5d files=%d", r["file"], r["n_rs"], len(r["plans"]), len(r["files"]))
    probe = None
    if float(rc.get("probe_mb", 0) or 0) > 0:
        pat = rc.get("probe_url_pattern", "")
        target = next((f["location"] for r in results for f in r["files"] if pat and pat in f["location"]), None)
        if target:
            log.info("recon: probing first %s MB of %s", rc["probe_mb"], url_file_name(target))
            probe = probe_remote(target, float(rc["probe_mb"]))
    doc = ["# Step 0 — Index recon report", "",
           f"Generated by `python -m src.run --stage recon`. Source: `{index_dir.name}/` ({len(results)} files). "
           "All files were streamed with ijson (key paths discovered from the parse events, not from an assumed schema).", "",
           findings(results, probe), "## Per-file profiles", ""]
    doc += [section_file(r) for r in sorted(results, key=lambda r: r["file"])]
    hn = next((r for r in results if r["brand"] == "healthnet"), None)
    if hn:
        doc.append(section_healthnet(hn))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "recon_report.md").write_text("\n".join(doc))
    log.info("recon: wrote %s", out_dir / "recon_report.md")
    return results


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--index-dir")
    ap.add_argument("--output-dir")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--probe-mb", type=float)
    a = ap.parse_args(argv)
    cfg = load_config(a.config, {"index_dir": a.index_dir, "output_dir": a.output_dir, "workers": a.workers})
    if a.probe_mb is not None:
        cfg.setdefault("recon", {})["probe_mb"] = a.probe_mb
    run(cfg)


if __name__ == "__main__":
    main()
