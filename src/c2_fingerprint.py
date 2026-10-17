import gzip
import json
import re
import urllib.request
from collections import Counter
from itertools import combinations

import ijson
import pandas as pd

from src.common import log

PREFIX_RE = re.compile(r"^\s*([A-Za-z][A-Za-z /&\-]{1,30}):")
NPPES_STATE_COL = "Provider Business Practice Location Address State Name"
CANDIDATE_LABELS = ("MIXED", "OTHER/UNKNOWN")


def open_stream(url, cap_bytes):
    req = urllib.request.Request(url, headers={"Range": f"bytes=0-{cap_bytes - 1}"})
    r = urllib.request.urlopen(req, timeout=120)
    return gzip.GzipFile(fileobj=r) if url.lower().endswith(".gz") else r, r


def stream_provider_refs(url, cap_mb, max_groups):
    npis, tins, nn, pref = set(), set(), Counter(), Counter()
    n_refs = n_groups = 0
    status, total = "complete", None
    try:
        stream, raw = open_stream(url, int(cap_mb * 1024 * 1024))
        total = raw.headers.get("Content-Range") or raw.headers.get("Content-Length")
        with raw:
            for pr in ijson.items(stream, "provider_references.item"):
                n_refs += 1
                nn["|".join(pr.get("network_name") or []) or "(empty)"] += 1
                for g in pr.get("provider_groups") or []:
                    n_groups += 1
                    npis.update(int(x) for x in g.get("npi") or [])
                    tin = g.get("tin") or {}
                    if tin.get("value"):
                        tins.add(str(tin["value"]))
                    m = PREFIX_RE.match(tin.get("business_name") or "")
                    pref[m.group(1).strip().upper() if m else "(no prefix)"] += 1
                if n_groups >= max_groups:
                    status = f"stopped at group cap {max_groups}"
                    break
    except ijson.IncompleteJSONError:
        status = f"stopped at byte cap {cap_mb} MB"
    except Exception as e:
        status = f"unreachable ({type(e).__name__}: {str(e)[:80]})"
    return {"status": status, "total_size": total, "provider_refs": n_refs, "provider_groups": n_groups, "n_npis": len(npis), "n_tins": len(tins),
            "network_name": dict(nn.most_common(20)), "business_name_prefixes": dict(pref.most_common(20))}, npis, tins


def nppes_state_share(npis, nppes_dir, state="CA"):
    if not npis or not nppes_dir or not nppes_dir.exists():
        return None, "nppes_dir missing"
    files = sorted(p for p in nppes_dir.glob("npidata_pfile_*.csv") if "fileheader" not in p.name.lower())
    if not files:
        return None, "npidata_pfile_*.csv not found"
    found = Counter()
    for chunk in pd.read_csv(files[-1], usecols=["NPI", NPPES_STATE_COL], dtype=str, chunksize=500_000):
        hit = chunk[chunk["NPI"].astype("int64").isin(npis)]
        found.update(hit[NPPES_STATE_COL].fillna("").str.upper())
    n = sum(found.values())
    return (round(found.get(state, 0) / n, 4) if n else None), f"{files[-1].name}: matched {n}/{len(npis)} NPIs"


def run(cfg, fcls):
    fp_cfg = (cfg.get("c2") or {}).get("fingerprint") or {}
    cap_mb, max_groups = float(fp_cfg.get("cap_mb", 40)), int(fp_cfg.get("max_groups", 200000))
    cache = cfg["output_dir"] / "c2" / "fingerprint_cache"
    cache.mkdir(parents=True, exist_ok=True)
    cand = fcls[(fcls["scope_verdict"] == "IN_SCOPE") & (fcls["network_label"].isin(CANDIDATE_LABELS) | (fcls["n_unknown_plans"] > 0))]
    log.info("fingerprint: %d candidate files (cap %s MB, %d groups)", len(cand), cap_mb, max_groups)
    rows, sets = [], {}
    for r in cand.itertuples(index=False):
        meta_p, npi_p = cache / f"{r.file_key}.json", cache / f"{r.file_key}_npis.parquet"
        if meta_p.exists() and npi_p.exists() and json.loads(meta_p.read_text()).get("cap_mb") == cap_mb:
            meta = json.loads(meta_p.read_text())
            npis = set(pd.read_parquet(npi_p)["npi"].tolist())
            log.info("fingerprint: cache hit %s", r.file_name)
        else:
            log.info("fingerprint: streaming %s", r.file_name)
            meta, npis, _ = stream_provider_refs(r.file_url, cap_mb, max_groups)
            meta["cap_mb"] = cap_mb
            share, note = nppes_state_share(npis, cfg.get("nppes_dir"))
            meta["nppes_ca_share"], meta["nppes_note"] = share, note
            meta_p.write_text(json.dumps(meta, indent=2, sort_keys=True))
            pd.DataFrame({"npi": sorted(npis)}).to_parquet(npi_p, index=False)
        sets[r.file_key] = npis
        pref = meta.get("business_name_prefixes", {})
        n_groups = meta.get("provider_groups") or 0
        rows.append({"file_key": r.file_key, "fp_status": meta["status"], "fp_total_size": meta.get("total_size"), "fp_provider_refs": meta["provider_refs"],
                     "fp_provider_groups": n_groups, "fp_npis": meta["n_npis"], "fp_tins": meta["n_tins"],
                     "fp_network_name": json.dumps(meta["network_name"], sort_keys=True), "fp_business_name_prefixes": json.dumps(pref, sort_keys=True),
                     "fp_ppo_prefix_share": round(pref.get("PPO", 0) / n_groups, 4) if n_groups else None,
                     "fp_nppes_ca_share": meta.get("nppes_ca_share"), "fp_nppes_note": meta.get("nppes_note")})
    out = pd.DataFrame(rows)
    if len(out):
        jac = {k: [] for k in sets}
        for a, b in combinations(sorted(sets), 2):
            j = len(sets[a] & sets[b]) / len(sets[a] | sets[b]) if sets[a] | sets[b] else 0.0
            jac[a].append(f"{b}:{j:.4f}")
            jac[b].append(f"{a}:{j:.4f}")
        out["fp_jaccard"] = out["file_key"].map(lambda k: "|".join(jac.get(k, [])) or "single candidate file")
        for r in out.itertuples(index=False):
            log.info("fingerprint %s: %s groups=%s npis=%s network_name=%s ppo_prefix_share=%s ca_share=%s", r.file_key, r.fp_status,
                     r.fp_provider_groups, r.fp_npis, r.fp_network_name, r.fp_ppo_prefix_share, r.fp_nppes_ca_share)
    return out
