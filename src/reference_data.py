from pathlib import Path

import pandas as pd

from src.common import log, resolve_path, stable_key

DEFAULT_FALLBACK_STATES = ["CA"]
CHUNK_ROWS = 500_000

NUCC_RENAME = {
    "Code": "taxonomy_code",
    "Grouping": "grouping",
    "Classification": "classification",
    "Specialization": "specialization",
    "Display Name": "display_name",
    "Section": "section",
}

DAC_RENAME = {
    "NPI": "npi",
    "Provider Last Name": "last_name",
    "Provider First Name": "first_name",
    "Cred": "credential",
    "pri_spec": "primary_specialty",
    "sec_spec_all": "secondary_specialties",
    "Facility Name": "facility_name",
    "org_pac_id": "org_pac_id",
    "adr_ln_1": "address_line1",
    "adr_ln_2": "address_line2",
    "City/Town": "city",
    "State": "state",
    "ZIP Code": "zip",
    "Telephone Number": "phone",
}

NPPES_RENAME = {
    "NPI": "npi",
    "Entity Type Code": "entity_type",
    "Employer Identification Number (EIN)": "ein",
    "Provider Organization Name (Legal Business Name)": "org_name",
    "Provider Last Name (Legal Name)": "last_name",
    "Provider First Name": "first_name",
    "Provider Credential Text": "credential",
    "Provider First Line Business Practice Location Address": "address_line1",
    "Provider Second Line Business Practice Location Address": "address_line2",
    "Provider Business Practice Location Address City Name": "city",
    "Provider Business Practice Location Address State Name": "state",
    "Provider Business Practice Location Address Postal Code": "zip",
    "Provider Business Practice Location Address Telephone Number": "phone",
    "Provider Enumeration Date": "enumeration_date",
    "Last Update Date": "last_update_date",
    "NPI Deactivation Date": "deactivation_date",
    "NPI Reactivation Date": "reactivation_date",
    "Healthcare Provider Taxonomy Code_1": "taxonomy_code",
    "Healthcare Provider Primary Taxonomy Switch_1": "taxonomy_primary",
}

PL_RENAME = {
    "NPI": "npi",
    "Provider Secondary Practice Location Address- Address Line 1": "address_line1",
    "Provider Secondary Practice Location Address-  Address Line 2": "address_line2",
    "Provider Secondary Practice Location Address - City Name": "city",
    "Provider Secondary Practice Location Address - State Name": "state",
    "Provider Secondary Practice Location Address - Postal Code": "zip",
    "Provider Secondary Practice Location Address - Telephone Number": "phone",
}

NPPES_DATE_COLS = ("enumeration_date", "last_update_date", "deactivation_date", "reactivation_date")


def npi_is_valid(npi):
    s = str(npi).strip() if npi is not None else ""
    if len(s) != 10 or not s.isdigit() or s[0] not in "12":
        return False
    total = 24
    for i, ch in enumerate(reversed(s[:9])):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            d = d - 9 if d > 9 else d
        total += d
    return (10 - total % 10) % 10 == int(s[9])


def find_file(directory, pattern):
    hits = sorted(Path(directory).glob(pattern))
    if not hits:
        raise FileNotFoundError(f"no file matching '{pattern}' in {directory}")
    if len(hits) > 1:
        log.warning("several files match '%s' in %s, using %s", pattern, directory, hits[-1].name)
    return hits[-1]


def ref_settings(cfg):
    return cfg.get("reference_data") or {}


def target_npis(cfg):
    paths = sorted((cfg["output_dir"] / "c4").glob("*/provider_refs.parquet"))
    if not paths:
        return None
    npis = set()
    for p in paths:
        npis.update(int(n) for n in pd.read_parquet(p, columns=["npi"])["npi"].dropna())
    log.info("reference scope: %s NPIs from %d provider_refs file(s)", f"{len(npis):,}", len(paths))
    return frozenset(npis)


def resolve_scope(cfg, npis=None, states=None):
    if npis is None and not states:
        npis = target_npis(cfg)
    if npis:
        npis = frozenset(int(n) for n in npis)
        return npis, None, f"npis-{stable_key(','.join(map(str, sorted(npis))), 12)}"
    if not states:
        log.warning("reference scope: no provider_refs yet, falling back to configured states")
    states = sorted({s.upper() for s in (states or ref_settings(cfg).get("fallback_states") or DEFAULT_FALLBACK_STATES)})
    return None, states, "states-" + "-".join(states)


def scope_mask(chunk, npi_col, state_col, npis, states):
    if npis is not None:
        return pd.to_numeric(chunk[npi_col], errors="coerce").isin(npis)
    return chunk[state_col].fillna("").str.upper().isin(states)


def cache_path(cfg, name):
    return cfg["output_dir"] / "_cache" / "reference" / name


def cached(path, builder, force=False):
    if path.exists() and not force:
        log.info("cache hit %s", path.name)
        return pd.read_parquet(path)
    df = builder()
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    log.info("cache write %s (%s rows)", path.name, f"{len(df):,}")
    return df


def stream_filtered(path, rename, npis, states, chunk_rows=CHUNK_ROWS):
    npi_col = next(k for k, v in rename.items() if v == "npi")
    state_col = next(k for k, v in rename.items() if v == "state")
    parts, scanned = [], 0
    for chunk in pd.read_csv(path, usecols=list(rename), dtype=str, chunksize=chunk_rows):
        scanned += len(chunk)
        parts.append(chunk[scope_mask(chunk, npi_col, state_col, npis, states)])
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=list(rename))
    log.info("streamed %s: %s rows scanned, %s kept", path.name, f"{scanned:,}", f"{len(df):,}")
    return finish(df.rename(columns=rename))


def finish(df):
    df["npi"] = pd.to_numeric(df["npi"], errors="coerce").astype("Int64")
    df["state"] = df["state"].fillna("").str.strip().str.upper()
    df["zip"] = df["zip"].fillna("").str.strip().str[:5]
    return df


def nppes_status(df):
    deact, react = df["deactivation_date"], df["reactivation_date"]
    status = pd.Series("active", index=df.index)
    status[deact.notna()] = "deactivated"
    status[deact.notna() & react.notna() & (react >= deact)] = "reactivated"
    return status


def load_nucc(cfg):
    path = find_file(cfg["reference_dir"], "nucc_taxonomy*.csv")
    df = pd.read_csv(path, dtype=str).fillna("").rename(columns=NUCC_RENAME)
    df = df[[c for c in NUCC_RENAME.values() if c in df.columns]]
    log.info("NUCC: %s taxonomy codes", f"{len(df):,}")
    return df


def benchmark_path(cfg):
    name = (cfg.get("c3") or {}).get("benchmark_xlsx", "")
    exact = cfg["reference_dir"] / name if name else None
    return exact if exact and exact.exists() else find_file(cfg["reference_dir"], "*Benchmarking*Hackathon*.xlsx")


def load_benchmark_taxonomy_xwalk(cfg):
    df = pd.read_excel(benchmark_path(cfg), sheet_name="Taxonomy-Spec Xwalk", dtype=str).fillna("")
    df.columns = ["spec_type", "spec_cd", "spec_descr", "taxonomy_code"]
    log.info("benchmark taxonomy xwalk: %s rows", f"{len(df):,}")
    return df


def load_benchmark_specialty_weights(cfg):
    df = pd.read_excel(benchmark_path(cfg), sheet_name="Specialty Weights", dtype={"Spec Code": str})
    df.columns = ["spec_cd", "specialty", "specialty_weight"]
    log.info("benchmark specialty weights: %s rows", f"{len(df):,}")
    return df


def load_benchmark_codes(cfg):
    df = pd.read_excel(benchmark_path(cfg), sheet_name="Spec-Proc Weights", dtype={"SPEC_CD": str, "PROC_CD": str})
    df.columns = ["spec_cd", "spec_description", "billing_code", "spec_proc_rank", "spec_proc_weight"]
    log.info("benchmark codes: %s rows, %s unique billing codes", f"{len(df):,}", f"{df['billing_code'].nunique():,}")
    return df


def load_dac(cfg, npis=None, states=None, force=False):
    ref = cfg["reference_dir"]
    npis, states, tag = resolve_scope(cfg, npis, states)
    raw = sorted(ref.glob("DAC_*ownloadable*.csv"))
    if raw:
        df = cached(cache_path(cfg, f"dac_{tag}.parquet"), lambda: stream_filtered(raw[-1], DAC_RENAME, npis, states, 200_000), force)
        df["dac_source"] = "national_file"
    else:
        legacy = ref / "dac_ca.parquet"
        if not legacy.exists():
            raise FileNotFoundError(f"neither DAC_*ownloadable*.csv nor dac_ca.parquet in {ref}")
        log.warning("DAC national file not found; using CA-only legacy cache %s (out-of-state DAC rows unavailable)", legacy.name)
        df = pd.read_parquet(legacy)
        df = df[df["npi"].isin(npis)] if npis is not None else df[df["state"].isin(states)]
        df = df.reset_index(drop=True)
        df["dac_source"] = "legacy_ca_only"
    log.info("DAC (%s): %s rows, %s NPIs, states %s", tag, f"{len(df):,}", f"{df['npi'].nunique():,}", df["state"].value_counts().head(5).to_dict())
    return df


def load_nppes(cfg, npis=None, states=None, force=False):
    nppes_dir = cfg["nppes_dir"]
    npis, states, tag = resolve_scope(cfg, npis, states)

    def build():
        df = stream_filtered(find_file(nppes_dir, "npidata_pfile_*[0-9].csv"), NPPES_RENAME, npis, states)
        for c in NPPES_DATE_COLS:
            df[c] = pd.to_datetime(df[c], errors="coerce", format="%m/%d/%Y")
        df["nppes_status"] = nppes_status(df)
        return df

    df = cached(cache_path(cfg, f"nppes_{tag}.parquet"), build, force)
    log.info("NPPES (%s): %s rows, status %s, states %s", tag, f"{len(df):,}", df["nppes_status"].value_counts().to_dict(),
             df["state"].value_counts().head(5).to_dict())
    return df


def load_pl(cfg, npis=None, states=None, force=False):
    nppes_dir = cfg["nppes_dir"]
    npis, states, tag = resolve_scope(cfg, npis, states)
    df = cached(cache_path(cfg, f"pl_{tag}.parquet"),
                lambda: stream_filtered(find_file(nppes_dir, "pl_pfile_*[0-9].csv"), PL_RENAME, npis, states, 200_000), force)
    log.info("PL (%s): %s rows, %s NPIs", tag, f"{len(df):,}", f"{df['npi'].nunique():,}")
    return df


def load_all(cfg, npis=None, states=None):
    npis, states, _ = resolve_scope(cfg, npis, states)
    return {
        "nucc": load_nucc(cfg),
        "benchmark_codes": load_benchmark_codes(cfg),
        "benchmark_spec_weights": load_benchmark_specialty_weights(cfg),
        "benchmark_tax_xwalk": load_benchmark_taxonomy_xwalk(cfg),
        "dac": load_dac(cfg, npis, states),
        "nppes": load_nppes(cfg, npis, states),
        "pl": load_pl(cfg, npis, states),
    }


CA_FACILITY_RENAME = {
    "NPI": "npi", "FACID": "facid", "CCN": "ccn", "FACNAME": "facility_name", "BUSINESS_NAME": "business_name", "ADDRESS": "address_line1",
    "CITY": "city", "ZIP": "zip", "COUNTY_NAME": "county", "FIPS_COUNTY_CODE": "fips_county", "FAC_TYPE_CODE": "fac_type", "LICENSE_STATUS_DESCRIPTION": "license_status",
    "LICENSED_CERTIFIED": "licensed_certified", "LATITUDE": "latitude", "LONGITUDE": "longitude", "HCAI_ID": "hcai_id", "DATA_DATE": "data_date",
}

IRS_EO_RENAME = {"EIN": "ein", "NAME": "name", "STREET": "address_line1", "CITY": "city", "STATE": "state", "ZIP": "zip", "NTEE_CD": "ntee_cd"}


def sources_dir(cfg):
    return resolve_path((cfg.get("c5") or {}).get("sources_dir", "raw/sources"))


def load_ca_facilities(cfg, active_only=True):
    path = find_file(sources_dir(cfg), "health_facility_locations*.csv")
    df = pd.read_csv(path, dtype=str, usecols=lambda c: c in CA_FACILITY_RENAME).rename(columns=CA_FACILITY_RENAME)
    if active_only:
        df = df[~df["license_status"].fillna("").str.upper().str.startswith("INACTIVE")]
    df["state"] = "CA"
    df = finish(df)
    df["data_date"] = pd.to_datetime(df["data_date"], errors="coerce", format="%m/%d/%Y")
    log.info("CA licensed facilities: %s rows, %s with NPI", f"{len(df):,}", f"{df['npi'].notna().sum():,}")
    return df.reset_index(drop=True)


def load_irs_eo(cfg, eins=None):
    path = find_file(sources_dir(cfg), "eo_*.csv")
    df = pd.read_csv(path, dtype=str, usecols=list(IRS_EO_RENAME)).rename(columns=IRS_EO_RENAME)
    if eins is not None:
        df = df[df["ein"].isin({str(e).replace("-", "").strip() for e in eins})]
    df["state"] = df["state"].fillna("").str.upper()
    df["zip"] = df["zip"].fillna("").str[:5]
    log.info("IRS exempt organisations (%s): %s rows", path.name, f"{len(df):,}")
    return df.reset_index(drop=True)


POS_CLIA_RENAME = {"PRVDR_NUM": "clia_id", "FAC_NAME": "facility_name", "ST_ADR": "address_line1", "ADDTNL_ST_ADR": "address_line2",
                   "CITY_NAME": "city", "STATE_CD": "state", "ZIP_CD": "zip", "PGM_TRMNTN_CD": "termination_code", "CRTFCT_TYPE_CD": "certificate_type"}


def load_pos_clia(cfg, zips=None):
    path = find_file(sources_dir(cfg), "POS_CLIA*.csv")
    keep = {str(z)[:5] for z in zips} if zips is not None else None
    parts, scanned = [], 0
    for chunk in pd.read_csv(path, dtype=str, usecols=list(POS_CLIA_RENAME), chunksize=200_000):
        scanned += len(chunk)
        chunk = chunk[chunk["PGM_TRMNTN_CD"] == "00"]
        if keep is not None:
            chunk = chunk[chunk["ZIP_CD"].fillna("").str[:5].isin(keep)]
        parts.append(chunk)
    df = pd.concat(parts, ignore_index=True).rename(columns=POS_CLIA_RENAME)
    df["state"] = df["state"].fillna("").str.upper()
    df["zip"] = df["zip"].fillna("").str[:5]
    log.info("CMS POS CLIA (%s): %s rows scanned, %s active sites kept", path.name, f"{scanned:,}", f"{len(df):,}")
    return df


def ca_county_names(cfg):
    fac = load_ca_facilities(cfg, active_only=False)
    m = fac[["fips_county", "county"]].dropna().drop_duplicates("fips_county")
    return dict(zip(m["fips_county"], m["county"].str.upper()))
