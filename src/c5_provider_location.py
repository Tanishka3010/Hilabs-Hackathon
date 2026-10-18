import json
import re

import pandas as pd

from src import geocode as geo
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


def strong_name_match(a, b, drop, threshold):
    ta, tb = name_tokens(a, drop), name_tokens(b, drop)
    shared = ta & tb
    return bool(ta and tb) and len(shared) >= 2 and len(shared) / len(ta | tb) >= threshold


def npi_name_zips(nppes, pl, entity_types=("2",)):
    people = (nppes["first_name"].fillna("").map(clean) + " " + nppes["last_name"].fillna("").map(clean)).str.strip()
    names = nppes.assign(match_name=nppes["org_name"].fillna("").map(clean).where(nppes["entity_type"] == "2", people))
    names = names[names["entity_type"].isin(entity_types) & (names["match_name"] != "")][["npi", "match_name", "zip"]]
    zips = pd.concat([names[["npi", "zip"]], pl[pl["npi"].isin(set(names["npi"]))][["npi", "zip"]]]).assign(zip=lambda d: d["zip"].map(clean).str[:5])
    return zips[zips["zip"] != ""].drop_duplicates().merge(names[["npi", "match_name"]], on="npi")


def match_sites(npi_zips, sites, c5, place_cols=("city",)):
    m = npi_zips.merge(sites.drop(columns=["npi"], errors="ignore"), on="zip")
    if not len(m):
        return m
    thr = float(c5["facility_name_match_threshold"])
    keep = [strong_name_match(o, f, place_words(*[r[c] for c in place_cols]), thr)
            for o, f, r in zip(m["match_name"], m["facility_name"], m[list(place_cols)].to_dict("records"))]
    return m[keep]


def place_words(*places):
    return frozenset(GEO_STOP | {w for p in places for w in re.sub(r"[^A-Z0-9 ]", " ", clean(p).upper()).split()})


def house_number(key):
    m = re.match(r"^(\d+)", key or "")
    return m.group(1) if m else ""


def safe_geocode(raw_key, canonical_key, match_type):
    same_number = house_number(raw_key) == house_number(canonical_key) or not house_number(raw_key)
    same_zip = raw_key[-5:] == canonical_key[-5:]
    return same_number and (match_type == "Exact" or same_zip)


def geocode_candidates(cand, cfg, c5):
    settings = c5.get("geocode") or {}
    if not settings.get("enabled", False) or not len(cand):
        return cand.assign(raw_address_key=cand["address_key"], geo_status="Not_Geocoded", geo_address="", longitude="", latitude="", county_fips="")
    reps = cand.drop_duplicates("address_key")
    reps = pd.DataFrame({"address_key": reps["address_key"], "street": [UNIT_RE.sub(" ", clean(a)).strip(" ,") for a in reps["address_line1"]],
                         "city": reps["city"], "state": reps["state"], "zip": reps["zip"]})
    g = geo.geocode(reps, cfg["output_dir"] / "_cache" / "geocode" / "census_results.parquet", settings)
    canon = [address_key(ga.split(", ")[0], "", ga.split(", ")[-1]) if st == "Match" and isinstance(ga, str) and ga else k
             for k, st, ga in zip(g["address_key"], g["geo_status"], g["geo_address"])]
    g["canonical_key"] = [c if safe_geocode(k, c, mt) else k for k, c, mt in zip(g["address_key"], canon, g["geo_match_type"].fillna(""))]
    g["geo_status"] = [st if st != "Match" or safe_geocode(k, c, mt) else "Rejected_Non_Exact"
                       for k, c, st, mt in zip(g["address_key"], canon, g["geo_status"], g["geo_match_type"].fillna(""))]
    g["county_fips"] = (g["state_fips"].fillna("") + g["county_fips"].fillna("")).where(g["geo_status"] == "Match", "")
    for col in ("geo_address", "longitude", "latitude"):
        g[col] = g[col].where(g["geo_status"] == "Match", "")
    keep = ["address_key", "canonical_key", "geo_status", "geo_address", "longitude", "latitude", "county_fips"]
    out = cand.merge(g[keep], on="address_key", how="left").rename(columns={"address_key": "raw_address_key", "canonical_key": "address_key"})
    out["address_key"] = out["address_key"].fillna(out["raw_address_key"])
    out["geo_status"] = out["geo_status"].fillna("Not_Geocoded")
    return out


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
    unmatched = nppes[~nppes["npi"].isin(set(by_npi["npi"].dropna().astype("int64")))]
    pool = fac[fac["npi"].isna()].assign(zip=lambda d: d["zip"].map(clean).str[:5])
    m = match_sites(npi_name_zips(unmatched, pl), pool, c5, ("city", "county"))
    by_name = m.assign(source="ca_facility_name_zip", source_name=m["facility_name"] if len(m) else "", source_date=m["data_date"] if len(m) else pd.NaT)
    return pd.concat([by_npi.reindex(columns=CANDIDATE_COLS), by_name.reindex(columns=CANDIDATE_COLS)], ignore_index=True)


def dac_group_candidates(dac_state, nppes, pl, c5):
    if dac_state is None or not len(dac_state) or not len(nppes):
        return pd.DataFrame(columns=CANDIDATE_COLS)
    groups = dac_state.assign(zip=dac_state["zip"].map(clean).str[:5], facility_name=dac_state["facility_name"].map(clean))
    groups = groups[groups["facility_name"] != ""].drop_duplicates(["facility_name", "address_line1", "address_line2", "zip"])
    m = match_sites(npi_name_zips(nppes, pl), groups, c5)
    if not len(m):
        return pd.DataFrame(columns=CANDIDATE_COLS)
    return m.assign(source="dac_group", source_name=m["facility_name"], source_date=pd.NaT).reindex(columns=CANDIDATE_COLS)


def clia_candidates(clia, nppes, pl, c5):
    if clia is None or not len(clia) or not len(nppes):
        return pd.DataFrame(columns=CANDIDATE_COLS)
    m = match_sites(npi_name_zips(nppes, pl, ("1", "2")), clia.assign(facility_name=clia["facility_name"].map(clean)), c5)
    if not len(m):
        return pd.DataFrame(columns=CANDIDATE_COLS)
    return m.assign(source="cms_clia", source_name=m["facility_name"], source_date=pd.NaT).reindex(columns=CANDIDATE_COLS)


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
            "geocoded": bool((g["geo_status"] == "Match").any()) if "geo_status" in g else False,
            "geo_address": next((x for x, st in zip(g.get("geo_address", []), g.get("geo_status", [])) if st == "Match"), ""),
            "latitude": next((x for x, st in zip(g.get("latitude", []), g.get("geo_status", [])) if st == "Match"), ""),
            "longitude": next((x for x, st in zip(g.get("longitude", []), g.get("geo_status", [])) if st == "Match"), ""),
            "county_fips": next((x for x, st in zip(g.get("county_fips", []), g.get("geo_status", [])) if st == "Match"), ""),
            "n_raw_spellings": g["raw_address_key"].nunique() if "raw_address_key" in g else 1,
            "registry_names": "|".join(sorted({n for n in g.loc[g["source"].isin(["ca_facility", "ca_facility_name_zip", "irs_eo", "dac_group", "cms_clia"]), "source_name"] if n})),
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
    a = a.sort_values(["npi", "in_target_state", "verified", "confidence", "geocoded", "nppes_last_update", "address_key"],
                      ascending=[True, False, False, False, False, False, True], na_position="last")
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
            "best_county": None if b is None else b.get("county_name"), "best_geocoded": None if b is None else bool(b["geocoded"]),
            "best_geo_address": None if b is None else b["geo_address"], "best_latitude": None if b is None else b["latitude"],
            "best_longitude": None if b is None else b["longitude"],
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


def county_label_check(refs, prov):
    lab = refs.assign(label=refs["business_name"].fillna("").str.extract(r"^\s*PPO:\s*(.+?)\s+County\s*$", flags=re.I)[0])
    lab = lab.dropna(subset=["label"])[["npi", "tin", "business_name", "label"]].drop_duplicates()
    if not len(lab):
        return pd.DataFrame(columns=["npi", "tin", "business_name", "label", "best_county", "result"])
    lab["label"] = lab["label"].str.upper().str.strip()
    lab = lab.merge(prov[["npi", "best_county", "location_status"]], on="npi", how="left")
    lab["result"] = ["no_county" if not isinstance(b, str) or not b else ("agrees" if b == l else "differs") for l, b in zip(lab["label"], lab["best_county"])]
    return lab.sort_values(["result", "label", "npi"]).reset_index(drop=True)


CHECK_COLS = ["sample_id", "stratum", "npi", "provider_name", "entity_type", "tin", "tin_business_name", "taxonomy_code", "our_best_address",
              "our_location_status", "our_confidence", "directory_found", "directory_address", "address_matches", "checked_by", "checked_on", "notes"]


def directory_check_sample(prov, nppes, refs, c5):
    dc = c5.get("directory_check") or {}
    n, seed = int(dc.get("per_stratum", 25)), int(dc.get("seed", 7))
    meta = nppes.set_index("npi")
    first_tin = refs.dropna(subset=["npi"]).sort_values(["npi", "tin"]).drop_duplicates("npi").set_index("npi")
    pool = prov[prov["best_address"].notna() & (prov["best_state"] == c5["target_state"])]
    strata = {"verified": pool[pool["location_status"] == "verified"], "unverified": pool[pool["location_status"].str.startswith("unverified")]}
    rows = []
    for name, g in strata.items():
        for r in g.sample(min(n, len(g)), random_state=seed).sort_values("npi").itertuples(index=False):
            m = meta.loc[r.npi] if r.npi in meta.index else None
            person = f"{clean(m['first_name'])} {clean(m['last_name'])}".strip() if m is not None else ""
            rows.append({"stratum": name, "npi": r.npi, "provider_name": (clean(m["org_name"]) or person) if m is not None else "",
                         "entity_type": r.entity_type, "tin": first_tin["tin"].get(r.npi, ""), "tin_business_name": first_tin["business_name"].get(r.npi, ""),
                         "taxonomy_code": r.taxonomy_code, "our_best_address": r.best_address, "our_location_status": r.location_status,
                         "our_confidence": r.best_confidence})
    df = pd.DataFrame(rows)
    df.insert(0, "sample_id", range(1, len(df) + 1))
    return df.reindex(columns=CHECK_COLS)


def score_directory_check(check):
    yes = {"Y", "YES", "TRUE", "1"}
    done = check[check["address_matches"].fillna("").astype(str).str.strip() != ""].copy()
    if not len(done):
        return {"checked": 0, "status": "no rows filled in yet"}
    done["ok"] = done["address_matches"].astype(str).str.strip().str.upper().isin(yes)
    found = done["directory_found"].fillna("").astype(str).str.strip().str.upper().isin(yes)
    out = {"checked": int(len(done)), "found_in_directory": int(found.sum())}
    for stratum, g in done.groupby("stratum"):
        out[f"precision_{stratum}"] = round(float(g["ok"].mean()), 4)
        out[f"checked_{stratum}"] = int(len(g))
    return out


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
    try:
        clia = rd.load_pos_clia(cfg, zips=set(nppes["zip"].map(clean).str[:5]) | set(pl["zip"].map(clean).str[:5]))
    except FileNotFoundError as e:
        log.warning("c5: CMS POS CLIA file unavailable (%s)", e)
        clia = None
    extra = [facility_candidates(fac, nppes, pl, npis, c5), irs_candidates(irs, refs), dac_group_candidates(dac_state, nppes, pl, c5),
             clia_candidates(clia, nppes, pl, c5)]
    cand = geocode_candidates(build_candidates(nppes, pl, dac, extra), cfg, c5)
    addr = score_addresses(cand, nppes, c5, as_of)
    try:
        counties = rd.ca_county_names(cfg)
    except FileNotFoundError:
        counties = {}
    addr["county_name"] = addr["county_fips"].map(counties) if len(addr) else []
    prov = provider_summary(npis, addr, nppes, c5)
    county_check = county_label_check(refs, prov)
    links = link_tin_npi_address(refs, addr, nppes, c5)
    cand.to_parquet(out / "address_candidates.parquet", index=False)
    addr.to_csv(out / "npi_addresses.csv", index=False)
    prov.to_csv(out / "provider_locations.csv", index=False)
    links.to_csv(out / "tin_npi_address.csv", index=False)
    county_check.to_csv(out / "county_label_check.csv", index=False)
    check_path = out / "directory_check_sample.csv"
    if not check_path.exists():
        directory_check_sample(prov, nppes, refs, c5).to_csv(check_path, index=False)
        log.info("c5: wrote %s for a manual Health Net directory spot check", check_path.name)
    directory_check = score_directory_check(pd.read_csv(check_path, dtype=str, keep_default_na=False))
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
        "geocode": {
            "distinct_raw_addresses": int(cand["raw_address_key"].nunique()) if "raw_address_key" in cand else 0,
            "raw_addresses_by_status": cand.drop_duplicates("raw_address_key")["geo_status"].value_counts().to_dict() if "raw_address_key" in cand else {},
            "npi_addresses_merged_from_several_spellings": int((addr["n_raw_spellings"] > 1).sum()) if len(addr) else 0,
            "best_addresses_geocoded": int(prov["best_geocoded"].fillna(False).sum()),
            "verified_best_addresses_geocoded": int(prov.loc[prov["location_status"] == "verified", "best_geocoded"].fillna(False).sum()),
        },
        "county_label_check": county_check["result"].value_counts().to_dict() if len(county_check) else {},
        "directory_check": directory_check,
        "config": {k: c5.get(k) for k in ("min_agreeing_sources", "source_weights", "source_family", "high_trust_sources", "recency_factors",
                                            "nppes_stale_years", "name_match_threshold", "facility_name_match_threshold")},
    }
    write_json(stats, out / "c5_stats.json")
    log.info("c5: %d NPIs, %d with an address, %d verified (%.1f%%); TINs verified %d of %d", stats["npis"], stats["npis_with_any_address"],
             stats["npis_with_verified_address"], 100 * stats["share_npis_verified"], stats["tins_with_verified_address"], stats["tins"])
    return stats
