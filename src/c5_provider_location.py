import json
import re

import pandas as pd

from src import geocode as geo
from src import reference_data as rd
from src.c3_quality import as_of_date
from src.common import log, resolve_path, stable_key, write_json

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


PO_BOX_RE = re.compile(r"^\s*P\.?\s*O\.?\s*BOX\b|^\s*POST OFFICE BOX\b", re.I)


def dca_candidates(dca, nppes, state="CA"):
    """CA licence boards (DCA): NPPES lists each clinician's licence numbers, so the licence links the NPI to the board's address of record
    (an identifier link, not a name match). The licensee's surname must also match NPPES, which guards against equal digits on different
    boards. PO boxes are dropped: they are not practice sites."""
    if dca is None or not len(dca) or not len(nppes) or "licences" not in nppes:
        return pd.DataFrame(columns=CANDIDATE_COLS)
    lic = nppes[["npi", "last_name", "licences"]].assign(licence=nppes["licences"].fillna("").str.split("|")).explode("licence")
    lic = lic[lic["licence"].fillna("").str.startswith(f"{state}:")]
    lic["licence_digits"] = lic["licence"].str.split(":").str[1]
    m = lic.merge(dca, on="licence_digits", suffixes=("", "_dca"))
    m = m[m["last_name"].fillna("").map(clean).str.upper() == m["last_name_dca"].fillna("").map(clean).str.upper()]
    m = m[~m["address_line1"].fillna("").str.match(PO_BOX_RE)]
    if not len(m):
        return pd.DataFrame(columns=CANDIDATE_COLS)
    return m.assign(source="ca_dca_licence", source_name=m["board"] + ": " + m["licence_type"], source_date=m["expiration_date"]).reindex(columns=CANDIDATE_COLS)


def ppef_dac_candidates(ppef, dac_state, nppes):
    """Organisation NPI -> PECOS PAC ID (CMS PPEF) -> every DAC row whose group practice has that PAC ID -> the group's Medicare-enrolled
    practice sites. This replaces a name + ZIP guess with an identifier chain, so the DAC group site is linked to the organisation's NPI."""
    if ppef is None or dac_state is None or not len(ppef) or not len(dac_state) or "org_pac_id" not in dac_state:
        return pd.DataFrame(columns=CANDIDATE_COLS)
    orgs = set(nppes.loc[nppes["entity_type"] == "2", "npi"].astype("int64")) if len(nppes) else set()
    p = ppef[ppef["enrollment_id"].fillna("").str.startswith("O") & ppef["npi"].isin(orgs)][["npi", "pac_id"]].drop_duplicates()
    sites = dac_state.dropna(subset=["org_pac_id"]).drop(columns=["npi"], errors="ignore").drop_duplicates(["org_pac_id", "address_line1", "address_line2", "zip"])
    m = p.merge(sites, left_on="pac_id", right_on="org_pac_id")
    if not len(m):
        return pd.DataFrame(columns=CANDIDATE_COLS)
    return m.assign(source="dac_pac_id", source_name=m["facility_name"], source_date=pd.NaT).reindex(columns=CANDIDATE_COLS)


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


def first_matched(g, col):
    if "geo_status" not in g or col not in g:
        return pd.Series(dtype=str)
    m = g[g["geo_status"] == "Match"].sort_values(["npi", "address_key", "source"])
    return m.groupby(["npi", "address_key"])[col].first()


def joined(df, col, by=("npi", "address_key")):
    sub = df[df[col].fillna("").astype(str) != ""]
    return sub.groupby(list(by))[col].agg(lambda s: "|".join(sorted(set(s.astype(str)))))


def score_addresses(cand, nppes, c5, as_of):
    """One row per (NPI, address key), built with grouped aggregations (no per-group Python loop).
    verified_strict: at least two source families linked to the NPI by an identifier (NPI or EIN) report the address, or the CA licence
    register lists it by NPI. corroborated: two families agree, but one of them was found by a name + ZIP search (its ZIP came from
    NPPES, so the agreement is weaker evidence)."""
    weights, family = c5["source_weights"], c5["source_family"]
    name_linked = set(c5.get("name_linked_sources") or ["ca_facility_name_zip", "dac_group", "cms_clia"])
    if not len(cand):
        return pd.DataFrame()
    status = nppes.set_index("npi")["nppes_status"] if len(nppes) else pd.Series(dtype=str)
    cand = cand.assign(source_family=cand["source"].map(family))
    cand["weight"] = [weights[s] * (recency_factor(d, as_of, c5) if s.startswith("nppes") else 1.0) for s, d in zip(cand["source"], cand["source_date"])]
    cand["id_family"] = cand["source_family"].where(~cand["source"].isin(name_linked))
    keys = ["npi", "address_key"]
    best_w = cand.groupby(keys + ["source"])["weight"].max().reset_index()
    best_w["miss"] = 1 - best_w["weight"]
    conf = 1 - best_w.groupby(keys)["miss"].prod()
    first = cand.sort_values(keys + ["source", "unit", "address_line1"]).groupby(keys).first()
    g = cand.groupby(keys)
    a = first[["address_line1", "address_line2", "city", "state", "zip"]].copy()
    geo = "geo_status" in cand
    parts = {
        "units": joined(cand, "unit"), "sources": best_w.groupby(keys)["source"].agg(lambda s: "|".join(sorted(s))),
        "source_families": g["source_family"].agg(lambda s: "|".join(sorted(set(s)))), "n_source_families": g["source_family"].nunique(),
        "n_id_linked_families": g["id_family"].nunique(), "confidence": conf.round(4),
        "nppes_last_update": cand[cand["source"].str.startswith("nppes")].groupby(keys)["source_date"].max(),
        "dac_facility_names": joined(cand[cand["source"] == "dac"], "source_name"),
        "geocoded": g["geo_status"].agg(lambda s: bool((s == "Match").any())) if geo else None,
        "geo_address": first_matched(cand, "geo_address"), "latitude": first_matched(cand, "latitude"), "longitude": first_matched(cand, "longitude"),
        "county_fips": first_matched(cand, "county_fips"),
        "n_raw_spellings": g["raw_address_key"].nunique() if "raw_address_key" in cand else None,
        "registry_names": joined(cand[cand["source"].isin(["ca_facility", "ca_facility_name_zip", "irs_eo", "dac_group", "dac_pac_id", "cms_clia", "ca_dca_licence"])], "source_name"),
        "irs_tins": joined(cand[cand["source"] == "irs_eo"], "source_tin"),
        "high_trust": g["source"].agg(lambda s: bool(s.isin(c5.get("high_trust_sources") or []).any())),
    }
    for col, ser in parts.items():
        a[col] = ser.reindex(a.index) if isinstance(ser, pd.Series) and len(ser) else (pd.NA if ser is not None and isinstance(ser, pd.Series) else ser)
    a["geocoded"] = a["geocoded"].fillna(False).astype(bool) if geo else False
    a["n_raw_spellings"] = a["n_raw_spellings"].fillna(1).astype(int) if "raw_address_key" in cand else 1
    a["nppes_last_update"] = pd.to_datetime(a["nppes_last_update"])
    a = a.reset_index()
    for col in ("units", "dac_facility_names", "geo_address", "latitude", "longitude", "county_fips", "registry_names", "irs_tins"):
        a[col] = a[col].fillna("")
    a["npi"] = a["npi"].astype("int64")
    a["address_id"] = a["address_key"].map(lambda k: stable_key(k, 12))
    a["in_target_state"] = a["state"] == c5["target_state"].upper()
    a["nppes_status"] = a["npi"].map(status).fillna("not_in_nppes")
    need = int(c5["min_agreeing_sources"])
    strict = ((a["n_id_linked_families"] >= need) | a["high_trust"]) & a["in_target_state"]
    corroborated = ~strict & (a["n_source_families"] >= need) & a["in_target_state"]
    a["verified"] = strict | corroborated
    a["verification_strength"] = ["strict" if s else ("corroborated" if c else "") for s, c in zip(strict, corroborated)]
    a["verification_basis"] = ["state_license_register" if (s and h and n < need) else ("two_id_linked_sources" if s else ("id_and_name_linked_sources" if c else ""))
                               for s, c, h, n in zip(strict, corroborated, a["high_trust"], a["n_id_linked_families"])]
    a["strength_rank"] = a["verification_strength"].map({"strict": 0, "corroborated": 1}).fillna(2)
    a = a.sort_values(["npi", "in_target_state", "strength_rank", "confidence", "geocoded", "nppes_last_update", "address_key"],
                      ascending=[True, False, True, False, False, False, True], na_position="last")
    a["rank"] = a.groupby("npi").cumcount() + 1
    cols = ["npi", "address_key", "address_id", "address_line1", "address_line2", "city", "state", "zip", "units", "sources", "source_families",
            "n_source_families", "n_id_linked_families", "confidence", "nppes_last_update", "dac_facility_names", "geocoded", "geo_address",
            "latitude", "longitude", "county_fips", "n_raw_spellings", "registry_names", "irs_tins", "high_trust", "in_target_state", "nppes_status",
            "verified", "verification_strength", "verification_basis", "rank"]
    return a[cols].reset_index(drop=True)


def provider_summary(npis, addr, nppes, c5):
    meta = nppes.drop_duplicates("npi").set_index("npi") if len(nppes) else pd.DataFrame(columns=["entity_type", "nppes_status", "taxonomy_code", "last_update_date"])
    p = pd.DataFrame({"npi": sorted(npis)})
    best = addr[addr["rank"] == 1].set_index("npi") if len(addr) else pd.DataFrame()
    counts = addr.groupby("npi").agg(n_addresses=("address_key", "size"), n_verified=("verified", "sum"),
                                     n_in_state=("in_target_state", "sum")) if len(addr) else pd.DataFrame(columns=["n_addresses", "n_verified", "n_in_state"])
    p["entity_type"] = p["npi"].map(meta["entity_type"]).fillna("") if len(meta) else ""
    p["nppes_status"] = p["npi"].map(meta["nppes_status"]).fillna("not_in_nppes") if len(meta) else "not_in_nppes"
    p["taxonomy_code"] = p["npi"].map(meta["taxonomy_code"]).fillna("").map(clean) if len(meta) else ""
    b = best.reindex(p["npi"]) if len(best) else pd.DataFrame(index=p["npi"])
    has = b["address_id"].notna().to_numpy() if "address_id" in b else pd.Series(False, index=p.index).to_numpy()
    k = counts.reindex(p["npi"]).fillna(0)
    status = []
    for st, h, strength, ins, n_in in zip(p["nppes_status"], has, b.get("verification_strength", pd.Series([""] * len(p))).fillna(""),
                                          b.get("in_target_state", pd.Series([False] * len(p))).fillna(False), k["n_in_state"]):
        if st == "deactivated":
            status.append("deactivated_npi")
        elif not h:
            status.append("no_address")
        elif strength == "strict":
            status.append("verified")
        elif strength == "corroborated":
            status.append("corroborated")
        elif not ins:
            status.append("out_of_state_only")
        elif int(n_in) > 1:
            status.append("unverified_multiple_candidates")
        else:
            status.append("unverified_single_source")
    p["location_status"] = status
    p["best_address_id"] = b["address_id"].to_numpy() if "address_id" in b else None
    p["best_address"] = [None if not h else ", ".join(x for x in [l1, l2, c, f"{s} {z}".strip()] if isinstance(x, str) and x)
                         for h, l1, l2, c, s, z in zip(has, b.get("address_line1", [None] * len(p)), b.get("address_line2", [None] * len(p)),
                                                      b.get("city", [None] * len(p)), b.get("state", [""] * len(p)), b.get("zip", [""] * len(p)))]
    for out_col, col in (("best_state", "state"), ("best_zip", "zip"), ("best_county", "county_name"), ("best_geocoded", "geocoded"),
                         ("best_geo_address", "geo_address"), ("best_latitude", "latitude"), ("best_longitude", "longitude"),
                         ("best_confidence", "confidence"), ("best_sources", "sources"), ("best_verification_strength", "verification_strength")):
        p[out_col] = b[col].to_numpy() if col in b else None
    p["n_addresses"] = k["n_addresses"].astype(int).to_numpy()
    p["n_verified_addresses"] = k["n_verified"].astype(int).to_numpy()
    p["nppes_last_update"] = p["npi"].map(meta["last_update_date"]) if len(meta) else None
    return p


def link_tin_npi_address(refs, addr, nppes, c5):
    """Every (TIN, NPI) pair gets the NPI's addresses, ranked so that addresses tied to that TIN come first.
    TIN-specific evidence: the IRS address of this EIN, a DAC group practice at the address whose name matches the TIN's business name,
    or TIN consensus (another NPI billing under the same TIN reports the same address). An NPPES legal-name match is recorded but does
    not tie an address to the TIN, so it is not counted as TIN-specific."""
    thr = float(c5["name_match_threshold"])
    org = nppes.drop_duplicates("npi").set_index("npi")["org_name"].map(clean) if len(nppes) else pd.Series(dtype=str)
    r = refs[["npi", "tin", "business_name"]].dropna(subset=["npi"]).assign(business_name=lambda d: d["business_name"].map(clean))
    names = r.groupby(["npi", "tin"], dropna=False)["business_name"].agg(lambda s: "|".join(sorted({x for x in s if x}))).rename("tin_business_names").reset_index()
    names["npi"] = names["npi"].astype("int64")
    names["org_name_similarity"] = [max((name_similarity(org.get(n, ""), b) for b in bn.split("|")), default=0.0) for n, bn in zip(names["npi"], names["tin_business_names"])]
    if not len(addr):
        return names.assign(address_id=None, pair_rank=None, link_evidence="no_address", dac_name_similarity=0.0, tin_consensus=0)
    m = names.merge(addr[["npi", "address_id", "address_key", "rank", "dac_facility_names", "irs_tins", "verified", "verification_strength"]], on="npi", how="left")
    has = m["address_id"].notna()
    cons = m[has].groupby(["tin", "address_key"], dropna=False)["npi"].nunique().rename("tin_consensus")
    m = m.merge(cons.reset_index(), on=["tin", "address_key"], how="left")
    m["tin_consensus"] = m["tin_consensus"].fillna(0).astype(int)
    m["dac_name_similarity"] = [0.0 if not isinstance(fs, str) or not fs else max((name_similarity(f, b) for f in fs.split("|") for b in bn.split("|") if f), default=0.0)
                                for fs, bn in zip(m["dac_facility_names"], m["tin_business_names"])]
    m["irs_hit"] = [isinstance(t, str) and isinstance(tin, str) and tin in t.split("|") for t, tin in zip(m["irs_tins"], m["tin"])]
    m["tin_specific"] = (m["dac_name_similarity"] >= thr) | m["irs_hit"] | (m["tin_consensus"] >= 2)
    m = m.sort_values(["tin", "npi", "tin_specific", "tin_consensus", "rank"], ascending=[True, True, False, False, True], na_position="last").reset_index(drop=True)
    m["pair_rank"] = (m.groupby(["tin", "npi"], dropna=False).cumcount() + 1).where(m["address_id"].notna())
    ev = []
    for d, i, c, o, v, h in zip(m["dac_name_similarity"], m["irs_hit"], m["tin_consensus"], m["org_name_similarity"], m["verified"], m["address_id"].notna()):
        if not h:
            ev.append("no_address")
            continue
        e = ["dac_group_name_matches_tin" if d >= thr else "", "irs_address_for_tin" if i else "", "shared_by_tin_npis" if c >= 2 else "",
             "nppes_org_name_matches_tin" if o >= thr else "", "verified_address" if v is True else ""]
        ev.append("+".join(x for x in e if x) or "npi_address_only")
    m["link_evidence"] = ev
    return m[["tin", "npi", "tin_business_names", "address_id", "pair_rank", "link_evidence", "tin_specific", "tin_consensus",
              "org_name_similarity", "dac_name_similarity"]].reset_index(drop=True)


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
    looked = check[check["directory_found"].fillna("").astype(str).str.strip() != ""]
    done = check[check["address_matches"].fillna("").astype(str).str.strip() != ""].copy()
    if not len(looked) and not len(done):
        return {"checked": 0, "status": "no rows filled in yet"}
    done["ok"] = done["address_matches"].astype(str).str.strip().str.upper().isin(yes)
    found = looked["directory_found"].astype(str).str.strip().str.upper().isin(yes)
    out = {"looked_up": int(len(looked)), "found_in_directory": int(found.sum()), "checked": int(len(done))}
    for stratum, g in done.groupby("stratum"):
        out[f"precision_{stratum}"] = round(float(g["ok"].mean()), 4)
        out[f"checked_{stratum}"] = int(len(g))
    return out


def load_source(name, loader, c5, missing):
    """Every source is required unless c5.allow_missing_sources is true: a silently skipped source would change the verified share."""
    try:
        return loader()
    except FileNotFoundError as e:
        if not c5.get("allow_missing_sources", False):
            raise FileNotFoundError(f"c5: required source '{name}' is missing ({e}). Put it in place, or set c5.allow_missing_sources: true "
                                    "to run without it (the run summary then lists it under missing_sources).") from e
        log.warning("c5: %s unavailable (%s); continuing because allow_missing_sources is true", name, e)
        missing.append(name)
        return None


def directory_check_path(cfg, c5):
    p = (c5.get("directory_check") or {}).get("path", "reference/directory_check_sample.csv")
    return resolve_path(p)


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
    all_npis = rd.target_npis(cfg) or set(refs["npi"])  # the rate files' full NPI set: C3, C5 and C6 share one reference cache
    excluded = sorted((cfg["output_dir"] / "c4").glob("*/non_ppo_exclusions.csv"))
    if excluded:
        ex = pd.concat([pd.read_csv(p, dtype={"tin": str}) for p in excluded])
        kept = pd.concat([pd.read_parquet(p, columns=["npi", "tin"]) for p in sorted((cfg["output_dir"] / "c4").glob("*/rates_selected.parquet"))]).drop_duplicates()
        drop = set(map(tuple, ex[["npi", "tin"]].astype({"npi": "int64"}).values)) - set(map(tuple, kept.dropna(subset=["npi"]).astype({"npi": "int64"}).values))
        refs = refs[[(n, t) not in drop for n, t in zip(refs["npi"], refs["tin"])]]
        log.info("c5: %d NPI-TIN pairs left out (only non-PPO evidence in C4)", len(drop))
    npis = set(refs["npi"])
    headers = [json.loads(p.read_text()) for p in (cfg["output_dir"] / "c4").glob("*/file_header.json")]
    as_of = as_of_date(cfg, cfg["c3"], headers)
    missing = []
    nppes = rd.load_nppes(cfg, npis=all_npis)
    nppes = nppes[nppes["npi"].astype("int64").isin(npis)]
    pl = rd.load_pl(cfg, npis=all_npis)
    pl = pl[pl["npi"].astype("int64").isin(npis)]
    empty_dac = pd.DataFrame(columns=["npi", "facility_name", "address_line1", "address_line2", "city", "state", "zip"])
    dac = load_source("dac", lambda: rd.load_dac(cfg, npis=all_npis), c5, missing)
    dac = empty_dac if dac is None else dac[dac["npi"].astype("int64").isin(npis)]
    nppes = nppes.drop_duplicates("npi").assign(npi=lambda d: d["npi"].astype("int64"))
    for df in (pl, dac):
        df["npi"] = df["npi"].astype("int64")
    fac = load_source("ca_facility", lambda: rd.load_ca_facilities(cfg), c5, missing)
    irs = load_source("irs_eo", lambda: rd.load_irs_eo(cfg, eins=set(refs["tin"].dropna())), c5, missing)
    dac_state = load_source("dac_state", lambda: rd.load_dac(cfg, states=[c5["target_state"]]), c5, missing)
    clia = load_source("cms_clia", lambda: rd.load_pos_clia(cfg, zips=set(nppes["zip"].map(clean).str[:5]) | set(pl["zip"].map(clean).str[:5])), c5, missing)
    dca = load_source("ca_dca_licence", lambda: rd.load_dca_licences(cfg), c5, missing)
    ppef = load_source("ppef", lambda: rd.load_ppef(cfg, npis=all_npis), c5, missing)
    extra = [facility_candidates(fac, nppes, pl, npis, c5), irs_candidates(irs, refs), dac_group_candidates(dac_state, nppes, pl, c5),
             clia_candidates(clia, nppes, pl, c5), dca_candidates(dca, nppes, c5["target_state"].upper()), ppef_dac_candidates(ppef, dac_state, nppes)]
    cand = geocode_candidates(build_candidates(nppes, pl, dac, extra), cfg, c5)
    addr = score_addresses(cand, nppes, c5, as_of)
    try:
        counties = rd.ca_county_names(cfg) if fac is not None else {}
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
    check_path = directory_check_path(cfg, c5)
    if not check_path.exists():
        check_path.parent.mkdir(parents=True, exist_ok=True)
        directory_check_sample(prov, nppes, refs, c5).to_csv(check_path, index=False)
        log.info("c5: wrote %s for a manual Health Net directory spot check", check_path)
    directory_check = score_directory_check(pd.read_csv(check_path, dtype=str, keep_default_na=False))
    # address_id is a hash of the address alone, so two NPIs at one building share it: look strengths up per (NPI, address)
    strength = addr.set_index(["npi", "address_id"])["verification_strength"] if len(addr) else pd.Series(dtype=str)
    links["strength"] = [strength.get((n, a), "") if isinstance(a, str) else "" for n, a in zip(links["npi"], links["address_id"])]
    links["strength"] = links["strength"].fillna("")
    per_pair = links.groupby(["tin", "npi"]).agg(strict=("strength", lambda s: (s == "strict").any()),
                                                 linked=("strength", lambda s: s.isin(["strict", "corroborated"]).any()))
    tins = links.groupby("tin").agg(any_address=("address_id", lambda s: s.notna().any()),
                                    any_npi_verified=("strength", lambda s: s.isin(["strict", "corroborated"]).any()),
                                    any_npi_strict=("strength", lambda s: (s == "strict").any()))
    depth = per_pair.groupby("tin").agg(all_npis_strict=("strict", "all"), all_npis_linked=("linked", "all"))
    tins = tins.join(depth)
    tin_spec = links[links["tin_specific"] & links["strength"].isin(["strict", "corroborated"])].groupby("tin").size()
    tins["tin_specific_verified"] = tins.index.isin(tin_spec.index)
    pairs = links.groupby(["tin", "npi"]).agg(any_address=("address_id", lambda s: s.notna().any()), tin_specific=("tin_specific", "any"))
    st = prov["location_status"]
    stats = {
        "as_of_date": as_of.isoformat(), "npis": len(npis), "tins": int(refs["tin"].nunique()), "npi_tin_pairs": int(len(pairs)),
        "missing_sources": missing,
        "address_candidates": int(len(cand)), "candidates_by_source": cand["source"].value_counts().to_dict(),
        "distinct_npi_addresses": int(len(addr)),
        "npis_by_location_status": st.value_counts().to_dict(),
        "npis_with_any_address": int((prov["n_addresses"] > 0).sum()),
        "npis_with_in_state_address": int((prov["best_state"] == c5["target_state"]).sum()),
        "npis_with_verified_address": int((st == "verified").sum()),
        "npis_with_corroborated_address": int((st == "corroborated").sum()),
        "share_npis_verified": round((st == "verified").mean(), 4),
        "share_npis_verified_or_corroborated": round(st.isin(["verified", "corroborated"]).mean(), 4),
        "verified_npis_by_basis": addr[(addr["rank"] == 1) & addr["verified"]]["verification_basis"].value_counts().to_dict() if len(addr) else {},
        "best_address_supported_only_by_nppes": int(((addr["rank"] == 1) & (addr["source_families"] == "nppes")).sum()) if len(addr) else 0,
        "verified_by_entity_type": prov.assign(v=st == "verified", c=st == "corroborated").groupby("entity_type")[["v", "c"]].sum().astype(int).to_dict("index"),
        "tins_with_any_address": int(tins["any_address"].sum()),
        "tins_with_verified_address": int(tins["any_npi_strict"].sum()),
        "tins_with_verified_or_corroborated_address": int(tins["any_npi_verified"].sum()),
        "tins_with_tin_specific_verified_address": int(tins["tin_specific_verified"].sum()),
        "share_tins_verified": round(tins["any_npi_strict"].mean(), 4) if len(tins) else None,
        "share_tins_verified_or_corroborated": round(tins["any_npi_verified"].mean(), 4) if len(tins) else None,
        "share_tins_tin_specific_verified": round(tins["tin_specific_verified"].mean(), 4) if len(tins) else None,
        "tins_all_npis_verified": int(tins["all_npis_strict"].sum()),
        "share_tins_all_npis_verified": round(tins["all_npis_strict"].mean(), 4) if len(tins) else None,
        "share_tins_all_npis_verified_or_corroborated": round(tins["all_npis_linked"].mean(), 4) if len(tins) else None,
        "npi_tin_pairs_with_tin_specific_address": int(pairs["tin_specific"].sum()),
        "link_evidence": links["link_evidence"].value_counts().to_dict(),
        "addresses_per_npi": prov["n_addresses"].describe().round(2).to_dict(),
        "geocode": {
            "distinct_raw_addresses": int(cand["raw_address_key"].nunique()) if "raw_address_key" in cand else 0,
            "raw_addresses_by_status": cand.drop_duplicates("raw_address_key")["geo_status"].value_counts().to_dict() if "raw_address_key" in cand else {},
            "npi_addresses_merged_from_several_spellings": int((addr["n_raw_spellings"] > 1).sum()) if len(addr) else 0,
            "best_addresses_geocoded": int(prov["best_geocoded"].fillna(False).astype(bool).sum()),
            "verified_best_addresses_geocoded": int(prov.loc[st == "verified", "best_geocoded"].fillna(False).astype(bool).sum()),
        },
        "county_label_check": county_check["result"].value_counts().to_dict() if len(county_check) else {},
        "directory_check": directory_check,
        "config": {k: c5.get(k) for k in ("min_agreeing_sources", "source_weights", "source_family", "high_trust_sources", "name_linked_sources",
                                            "recency_factors", "nppes_stale_years", "name_match_threshold", "facility_name_match_threshold")},
    }
    write_json(stats, out / "c5_stats.json")
    log.info("c5: %d NPIs, %d with an address, %d verified + %d corroborated (%.1f%% / %.1f%%); TINs verified %d of %d", stats["npis"],
             stats["npis_with_any_address"], stats["npis_with_verified_address"], stats["npis_with_corroborated_address"],
             100 * stats["share_npis_verified"], 100 * stats["share_npis_verified_or_corroborated"], stats["tins_with_verified_address"], stats["tins"])
    return stats
