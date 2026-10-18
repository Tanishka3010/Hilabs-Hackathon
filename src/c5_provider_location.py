import json
import re

import pandas as pd

from src import reference_data as rd
from src.c3_quality import as_of_date
from src.common import log, stable_key, write_json

STREET_ABBR = {
    "STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "BOULEVARD": "BLVD", "ROAD": "RD", "DRIVE": "DR", "LANE": "LN", "COURT": "CT",
    "PLACE": "PL", "PLAZA": "PLZ", "PARKWAY": "PKWY", "HIGHWAY": "HWY", "CIRCLE": "CIR", "TERRACE": "TER", "SQUARE": "SQ",
    "CENTER": "CTR", "WAY": "WAY", "TRAIL": "TRL", "EXPRESSWAY": "EXPY", "FREEWAY": "FWY", "NORTH": "N", "SOUTH": "S",
    "EAST": "E", "WEST": "W", "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW", "MOUNT": "MT",
    "SAINT": "ST", "FIRST": "1ST", "SECOND": "2ND", "THIRD": "3RD", "FOURTH": "4TH", "FIFTH": "5TH",
}
UNIT_RE = re.compile(r"\b(STE|SUITE|UNIT|APT|RM|ROOM|BLDG|BUILDING|FL|FLOOR|DEPT|SPC|TRLR)\b\.?\s*[A-Z0-9-]*|#\s*[A-Z0-9-]+", re.I)
NAME_STOP = {"INC", "LLC", "LLP", "LP", "PC", "PA", "CORP", "CORPORATION", "CO", "COMPANY", "THE", "OF", "AND", "A", "AN", "MD", "DO",
             "MEDICAL", "GROUP", "PROFESSIONAL", "ASSOCIATES", "HEALTH", "CARE", "SERVICES", "CENTER", "CTR", "INCORPORATED"}
PREFIX_RE = re.compile(r"^\s*([A-Za-z][A-Za-z /&\-]{1,40}):\s*")
CANDIDATE_COLS = ["npi", "source", "source_family", "address_line1", "address_line2", "city", "state", "zip", "source_name", "source_date", "source_tin"]


def clean(v):
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip().upper() in ("NAN", "NONE", "<NA>") else str(v).strip()


def normalize_street(line1, line2=""):
    s = f"{clean(line1)} {clean(line2)}".upper()
    s = UNIT_RE.sub(" ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    tokens = [STREET_ABBR.get(t, t) for t in s.split()]
    return " ".join(tokens)


def unit_of(line1, line2=""):
    return " ".join(re.sub(r"\s+", " ", m.group(0)).strip() for m in UNIT_RE.finditer(f"{clean(line1)} {clean(line2)}".upper()))


def address_key(line1, line2, zip5):
    street = normalize_street(line1, line2)
    z = clean(zip5)[:5]
    return f"{street}|{z}" if street and z else ""


GEO_STOP = {"COUNTY", "CALIFORNIA", "CA", "VALLEY", "CITY", "NORTH", "SOUTH", "EAST", "WEST", "CENTRAL", "SAN", "SANTA", "LOS", "LA", "EL", "DEL"}


def stem(t):
    return t[:-1] if len(t) > 4 and t.endswith("S") and not t.endswith("SS") else t


def name_tokens(name, drop=frozenset()):
    s = PREFIX_RE.sub("", clean(name).upper())
    return {stem(t) for t in re.sub(r"[^A-Z0-9 ]", " ", s).split() if t not in NAME_STOP and t not in drop and len(t) > 1}


def name_similarity(a, b, drop=frozenset()):
    ta, tb = name_tokens(a, drop), name_tokens(b, drop)
    return round(len(ta & tb) / len(ta | tb), 4) if ta and tb else 0.0


def place_words(*places):
    return frozenset(GEO_STOP | {w for p in places for w in re.sub(r"[^A-Z0-9 ]", " ", clean(p).upper()).split()})


def recency_factor(update_date, as_of, c5):
    rf = c5["recency_factors"]
    if update_date is None or pd.isna(update_date):
        return rf["stale"]
    years = (pd.Timestamp(as_of) - pd.Timestamp(update_date)).days / 365.25
    if years <= rf["fresh_years"]:
        return rf["fresh"]
    return rf["recent"] if years <= c5["nppes_stale_years"] else rf["stale"]


def facility_candidates(fac, nppes, pl, npis, c5):
    if fac is None or not len(fac):
        return pd.DataFrame(columns=CANDIDATE_COLS)
    by_npi = fac[fac["npi"].isin(npis)].assign(source="ca_facility", source_name=fac["facility_name"], source_date=fac["data_date"])
    orgs = nppes[(nppes["entity_type"] == "2") & ~nppes["npi"].isin(set(by_npi["npi"].dropna().astype("int64")))][["npi", "org_name", "zip"]]
    zips = pd.concat([orgs[["npi", "zip"]], pl[pl["npi"].isin(set(orgs["npi"]))][["npi", "zip"]]]).assign(zip=lambda d: d["zip"].map(clean).str[:5])
    zips = zips.drop_duplicates().merge(orgs[["npi", "org_name"]], on="npi")
    pool = fac[fac["npi"].isna()].assign(zip=lambda d: d["zip"].map(clean).str[:5])
    m = zips.merge(pool.drop(columns=["npi"]), on="zip")
    thr = float(c5["facility_name_match_threshold"])
    if len(m):
        m["sim"] = [name_similarity(o, f, place_words(city, county)) for o, f, city, county in zip(m["org_name"], m["facility_name"], m["city"], m["county"])]
        m = m[m["sim"] >= thr]
    by_name = m.assign(source="ca_facility_name_zip", source_name=m["facility_name"] if len(m) else "", source_date=m["data_date"] if len(m) else pd.NaT)
    return pd.concat([by_npi.reindex(columns=CANDIDATE_COLS), by_name.reindex(columns=CANDIDATE_COLS)], ignore_index=True)


def dac_group_candidates(dac_state, nppes, pl, c5):
    if dac_state is None or not len(dac_state) or not len(nppes):
        return pd.DataFrame(columns=CANDIDATE_COLS)
    orgs = nppes[nppes["entity_type"] == "2"][["npi", "org_name", "zip"]]
    zips = pd.concat([orgs[["npi", "zip"]], pl[pl["npi"].isin(set(orgs["npi"]))][["npi", "zip"]]]).assign(zip=lambda d: d["zip"].map(clean).str[:5])
    zips = zips[zips["zip"] != ""].drop_duplicates().merge(orgs[["npi", "org_name"]], on="npi")
    groups = dac_state.assign(zip=dac_state["zip"].map(clean).str[:5], facility_name=dac_state["facility_name"].map(clean))
    groups = groups[groups["facility_name"] != ""].drop_duplicates(["facility_name", "address_line1", "address_line2", "zip"])
    m = zips.merge(groups.drop(columns=["npi"]), on="zip")
    if not len(m):
        return pd.DataFrame(columns=CANDIDATE_COLS)
    thr = float(c5["facility_name_match_threshold"])
    m["sim"] = [name_similarity(o, f, place_words(city)) for o, f, city in zip(m["org_name"], m["facility_name"], m["city"])]
    m = m[m["sim"] >= thr]
    return m.assign(source="dac_group", source_name=m["facility_name"], source_date=pd.NaT).reindex(columns=CANDIDATE_COLS)


def irs_candidates(irs, refs):
    if irs is None or not len(irs):
        return pd.DataFrame(columns=CANDIDATE_COLS)
    pairs = refs[["npi", "tin"]].dropna().drop_duplicates().assign(ein=lambda d: d["tin"].str.replace("-", "", regex=False).str.strip())
    m = pairs.merge(irs, on="ein")
    return m.assign(source="irs_eo", source_name=m["name"], source_date=pd.NaT, source_tin=m["tin"]).reindex(columns=CANDIDATE_COLS)


def build_candidates(nppes, pl, dac, extra=()):
    parts = []
    if len(nppes):
        n = nppes.assign(source="nppes_primary", source_name=nppes["org_name"].fillna("").where(nppes["entity_type"] == "2",
                         (nppes["first_name"].fillna("") + " " + nppes["last_name"].fillna("")).str.strip()), source_date=nppes["last_update_date"])
        parts.append(n)
    if len(pl):
        upd = nppes.set_index("npi")["last_update_date"] if len(nppes) else pd.Series(dtype="datetime64[ns]")
        parts.append(pl.assign(source="nppes_secondary", source_name="", source_date=pl["npi"].map(upd)))
    if len(dac):
        parts.append(dac.assign(source="dac", source_name=dac["facility_name"], source_date=pd.NaT))
    parts += [e for e in extra if e is not None and len(e)]
    if not parts:
        return pd.DataFrame(columns=CANDIDATE_COLS + ["address_key", "unit"])
    c = pd.concat([p.reindex(columns=CANDIDATE_COLS) for p in parts], ignore_index=True)
    for col in ("address_line1", "address_line2", "city", "state", "zip", "source_name", "source_tin"):
        c[col] = c[col].map(clean)
    c["state"] = c["state"].str.upper()
    c["zip"] = c["zip"].str[:5]
    c["address_key"] = [address_key(a, b, z) for a, b, z in zip(c["address_line1"], c["address_line2"], c["zip"])]
    c["unit"] = [unit_of(a, b) for a, b in zip(c["address_line1"], c["address_line2"])]
    return c[c["address_key"] != ""].reset_index(drop=True)


def score_addresses(cand, nppes, c5, as_of):
    weights, family = c5["source_weights"], c5["source_family"]
    status = nppes.set_index("npi")["nppes_status"] if len(nppes) else pd.Series(dtype=str)
    cand = cand.assign(source_family=cand["source"].map(family))
    cand["weight"] = [weights[s] * (recency_factor(d, as_of, c5) if s.startswith("nppes") else 1.0) for s, d in zip(cand["source"], cand["source_date"])]
    rows = []
    for (npi, key), g in cand.groupby(["npi", "address_key"], sort=True):
        best_w = g.groupby("source")["weight"].max()
        conf = 1.0
        for w in best_w:
            conf *= 1 - w
        first = g.sort_values(["source", "unit"]).iloc[0]
        rows.append({
            "npi": int(npi), "address_key": key, "address_id": stable_key(key, 12),
            "address_line1": first["address_line1"], "address_line2": first["address_line2"], "city": first["city"], "state": first["state"],
            "zip": first["zip"], "units": "|".join(sorted({u for u in g["unit"] if u})),
            "sources": "|".join(sorted(best_w.index)), "source_families": "|".join(sorted(set(g["source_family"]))),
            "n_source_families": g["source_family"].nunique(), "confidence": round(1 - conf, 4),
            "nppes_last_update": g.loc[g["source"].str.startswith("nppes"), "source_date"].max(),
            "dac_facility_names": "|".join(sorted({n for n in g.loc[g["source"] == "dac", "source_name"] if n})),
            "registry_names": "|".join(sorted({n for n in g.loc[g["source"].isin(["ca_facility", "ca_facility_name_zip", "irs_eo"]), "source_name"] if n})),
            "irs_tins": "|".join(sorted({t for t in g.loc[g["source"] == "irs_eo", "source_tin"] if t})),
            "high_trust": bool(g["source"].isin(c5.get("high_trust_sources") or []).any()),
            "in_target_state": first["state"] == c5["target_state"].upper(),
            "nppes_status": status.get(npi, "not_in_nppes"),
        })
    a = pd.DataFrame(rows)
    if a.empty:
        return a
    two = a["n_source_families"] >= int(c5["min_agreeing_sources"])
    a["verified"] = (two | a["high_trust"]) & a["in_target_state"]
    a["verification_basis"] = [("two_independent_sources" if t else "state_license_register") if v else "" for v, t in zip(a["verified"], two)]
    a = a.sort_values(["npi", "in_target_state", "verified", "confidence", "nppes_last_update", "address_key"],
                      ascending=[True, False, False, False, False, True], na_position="last")
    a["rank"] = a.groupby("npi").cumcount() + 1
    return a.reset_index(drop=True)


def provider_summary(npis, addr, nppes, c5):
    meta = nppes.set_index("npi") if len(nppes) else pd.DataFrame()
    best = addr[addr["rank"] == 1].set_index("npi") if len(addr) else pd.DataFrame()
    counts = addr.groupby("npi").agg(n_addresses=("address_key", "size"), n_verified=("verified", "sum"),
                                     n_in_state=("in_target_state", "sum")) if len(addr) else pd.DataFrame()
    rows = []
    for npi in sorted(npis):
        m = meta.loc[npi] if npi in meta.index else None
        b = best.loc[npi] if npi in best.index else None
        k = counts.loc[npi] if npi in counts.index else None
        st = "not_in_nppes" if m is None else m["nppes_status"]
        if st == "deactivated":
            status = "deactivated_npi"
        elif b is None:
            status = "no_address"
        elif b["verified"]:
            status = "verified"
        elif not b["in_target_state"]:
            status = "out_of_state_only"
        elif int(k["n_in_state"]) > 1:
            status = "unverified_multiple_candidates"
        else:
            status = "unverified_single_source"
        rows.append({
            "npi": npi, "entity_type": "" if m is None else m["entity_type"], "nppes_status": st,
            "taxonomy_code": "" if m is None else clean(m["taxonomy_code"]),
            "location_status": status, "best_address_id": None if b is None else b["address_id"],
            "best_address": None if b is None else ", ".join(x for x in [b["address_line1"], b["address_line2"], b["city"], f"{b['state']} {b['zip']}".strip()] if x),
            "best_state": None if b is None else b["state"], "best_zip": None if b is None else b["zip"],
            "best_confidence": None if b is None else b["confidence"], "best_sources": None if b is None else b["sources"],
            "n_addresses": 0 if k is None else int(k["n_addresses"]), "n_verified_addresses": 0 if k is None else int(k["n_verified"]),
            "nppes_last_update": None if m is None else m["last_update_date"],
        })
    return pd.DataFrame(rows)


def link_tin_npi_address(refs, addr, nppes, c5):
    org = nppes.set_index("npi")["org_name"].map(clean) if len(nppes) else pd.Series(dtype=str)
    pairs = refs[["npi", "tin", "business_name"]].dropna(subset=["npi"]).drop_duplicates(["npi", "tin"])
    pairs["business_name"] = pairs["business_name"].map(clean)
    names = refs.groupby(["npi", "tin"])["business_name"].agg(lambda s: "|".join(sorted({clean(x) for x in s if clean(x)})))
    by_npi = {k: g for k, g in addr.groupby("npi")} if len(addr) else {}
    thr = float(c5["name_match_threshold"])
    rows = []
    for p in pairs.itertuples(index=False):
        npi, tin = int(p.npi), p.tin
        tin_names = names.get((p.npi, tin), "").split("|")
        org_sim = max((name_similarity(org.get(npi, ""), n) for n in tin_names), default=0.0)
        g = by_npi.get(npi)
        if g is None:
            rows.append({"tin": tin, "npi": npi, "tin_business_names": "|".join(tin_names), "address_id": None, "pair_rank": None,
                         "link_evidence": "no_address", "org_name_similarity": org_sim, "dac_name_similarity": 0.0})
            continue
        sims = [max((name_similarity(f, n) for f in fs.split("|") for n in tin_names if f), default=0.0) for fs in g["dac_facility_names"]]
        irs_hit = [tin in str(t).split("|") for t in g["irs_tins"]]
        g = g.assign(dac_name_similarity=sims, irs_hit=irs_hit, tin_specific=[s >= thr or h for s, h in zip(sims, irs_hit)])
        g = g.sort_values(["tin_specific", "rank"], ascending=[False, True])
        for i, a in enumerate(g.itertuples(index=False), start=1):
            ev = ["dac_group_name_matches_tin" if a.dac_name_similarity >= thr else "", "irs_address_for_tin" if a.irs_hit else "",
                  "nppes_org_name_matches_tin" if org_sim >= thr else "",
                  "verified_address" if a.verified else ""]
            rows.append({"tin": tin, "npi": npi, "tin_business_names": "|".join(tin_names), "address_id": a.address_id, "pair_rank": i,
                         "link_evidence": "+".join(e for e in ev if e) or "npi_address_only", "org_name_similarity": org_sim,
                         "dac_name_similarity": a.dac_name_similarity})
    return pd.DataFrame(rows)


def run(cfg):
    c5 = cfg["c5"]
    out = cfg["output_dir"] / "c5"
    out.mkdir(parents=True, exist_ok=True)
    ref_paths = sorted((cfg["output_dir"] / "c4").glob("*/provider_refs*.parquet"))
    if not ref_paths:
        log.warning("c5: no outputs/c4/*/provider_refs.parquet; run --stage c4 first")
        return {}
    refs = pd.concat([pd.read_parquet(p) for p in ref_paths], ignore_index=True)
    refs = refs.dropna(subset=["npi"]).assign(npi=lambda d: d["npi"].astype("int64"))
    npis = set(refs["npi"])
    headers = [json.loads(p.read_text()) for p in (cfg["output_dir"] / "c4").glob("*/file_header.json")]
    as_of = as_of_date(cfg, cfg["c3"], headers)
    nppes = rd.load_nppes(cfg, npis=npis)
    pl = rd.load_pl(cfg, npis=npis)
    try:
        dac = rd.load_dac(cfg, npis=npis)
    except FileNotFoundError as e:
        log.warning("c5: DAC unavailable (%s)", e)
        dac = pd.DataFrame(columns=["npi", "facility_name", "address_line1", "address_line2", "city", "state", "zip"])
    nppes = nppes.drop_duplicates("npi").assign(npi=lambda d: d["npi"].astype("int64"))
    for df in (pl, dac):
        df["npi"] = df["npi"].astype("int64")
    try:
        fac = rd.load_ca_facilities(cfg)
    except FileNotFoundError as e:
        log.warning("c5: CA facility list unavailable (%s)", e)
        fac = None
    try:
        irs = rd.load_irs_eo(cfg, eins=set(refs["tin"].dropna()))
    except FileNotFoundError as e:
        log.warning("c5: IRS EO file unavailable (%s)", e)
        irs = None
    try:
        dac_state = rd.load_dac(cfg, states=[c5["target_state"]])
    except FileNotFoundError as e:
        log.warning("c5: DAC state file unavailable for group matching (%s)", e)
        dac_state = None
    extra = [facility_candidates(fac, nppes, pl, npis, c5), irs_candidates(irs, refs), dac_group_candidates(dac_state, nppes, pl, c5)]
    cand = build_candidates(nppes, pl, dac, extra)
    addr = score_addresses(cand, nppes, c5, as_of)
    prov = provider_summary(npis, addr, nppes, c5)
    links = link_tin_npi_address(refs, addr, nppes, c5)
    cand.to_parquet(out / "address_candidates.parquet", index=False)
    addr.to_csv(out / "npi_addresses.csv", index=False)
    prov.to_csv(out / "provider_locations.csv", index=False)
    links.to_csv(out / "tin_npi_address.csv", index=False)
    tins = links.groupby("tin").agg(any_address=("address_id", lambda s: s.notna().any()),
                                    verified=("link_evidence", lambda s: s.str.contains("verified_address").any()))
    pairs = links.groupby(["tin", "npi"]).agg(any_address=("address_id", lambda s: s.notna().any()),
                                              tin_specific=("link_evidence", lambda s: s.str.contains("matches_tin|irs_address_for_tin").any()))
    stats = {
        "as_of_date": as_of.isoformat(), "npis": len(npis), "tins": int(refs["tin"].nunique()), "npi_tin_pairs": int(len(pairs)),
        "address_candidates": int(len(cand)), "candidates_by_source": cand["source"].value_counts().to_dict(),
        "distinct_npi_addresses": int(len(addr)),
        "npis_by_location_status": prov["location_status"].value_counts().to_dict(),
        "npis_with_any_address": int((prov["n_addresses"] > 0).sum()),
        "npis_with_in_state_address": int((prov["best_state"] == c5["target_state"]).sum()),
        "npis_with_verified_address": int((prov["location_status"] == "verified").sum()),
        "verified_npis_by_basis": addr[(addr["rank"] == 1) & addr["verified"]]["verification_basis"].value_counts().to_dict() if len(addr) else {},
        "share_npis_verified": round((prov["location_status"] == "verified").mean(), 4),
        "verified_by_entity_type": prov.assign(v=prov["location_status"] == "verified").groupby("entity_type")["v"].agg(["sum", "count"]).to_dict("index"),
        "tins_with_any_address": int(tins["any_address"].sum()), "tins_with_verified_address": int(tins["verified"].sum()),
        "share_tins_verified": round(tins["verified"].mean(), 4) if len(tins) else None,
        "npi_tin_pairs_with_tin_specific_address": int(pairs["tin_specific"].sum()),
        "link_evidence": links["link_evidence"].value_counts().to_dict(),
        "addresses_per_npi": prov["n_addresses"].describe().round(2).to_dict(),
        "config": {k: c5.get(k) for k in ("min_agreeing_sources", "source_weights", "source_family", "high_trust_sources", "recency_factors",
                                            "nppes_stale_years", "name_match_threshold", "facility_name_match_threshold")},
    }
    write_json(stats, out / "c5_stats.json")
    log.info("c5: %d NPIs, %d with an address, %d verified (%.1f%%); TINs verified %d of %d", stats["npis"], stats["npis_with_any_address"],
             stats["npis_with_verified_address"], 100 * stats["share_npis_verified"], stats["tins_with_verified_address"], stats["tins"])
    return stats
