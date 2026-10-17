"""
reference_data.py  —  Person B: loaders for every reference dataset.

Each public function takes `cfg` (the merged config dict) and returns a
DataFrame.  Heavy files (NPPES npidata ~11 GB, pl_pfile ~120 MB, DAC ~230 MB)
are filtered to California and cached as parquet so re-runs are instant.

This module is **owned by Person B** (see docs/work_split.md §1).
"""

from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd

from src.common import log

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_file(directory: Path, pattern: str) -> Path:
    """Return the single file matching *pattern* inside *directory*."""
    hits = sorted(directory.glob(pattern))
    if not hits:
        raise FileNotFoundError(f"No file matching '{pattern}' in {directory}")
    if len(hits) > 1:
        log.warning("Multiple files match '%s' in %s – using %s", pattern, directory, hits[0].name)
    return hits[0]


def _cached(path: Path, builder, *, force: bool = False) -> pd.DataFrame:
    """Return parquet at *path* if it exists, else call *builder* → df, save, return."""
    if path.exists() and not force:
        log.info("cache hit  → %s", path)
        return pd.read_parquet(path)
    df = builder()
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    log.info("cache miss → wrote %s  (%s rows)", path, f"{len(df):,}")
    return df


# ───────────────────────────────────────────────────────────────────────────
# 1.  NUCC Taxonomy
# ───────────────────────────────────────────────────────────────────────────

def load_nucc(cfg: dict) -> pd.DataFrame:
    """
    Load the NUCC taxonomy CSV.

    Returns columns:
        taxonomy_code, grouping, classification, specialization,
        display_name, section
    """
    ref_dir: Path = cfg["reference_dir"]
    path = _find_file(ref_dir, "nucc_taxonomy*.*")
    log.info("Loading NUCC taxonomy from %s", path.name)

    df = pd.read_csv(path, dtype=str).fillna("")

    # Normalise column names coming from the public NUCC CSV
    rename = {
        "Code": "taxonomy_code",
        "Grouping": "grouping",
        "Classification": "classification",
        "Specialization": "specialization",
        "Display Name": "display_name",
        "Section": "section",
    }
    df = df.rename(columns=rename)

    # Keep only the columns we need (extras like Definition, Notes are dropped)
    keep = [c for c in rename.values() if c in df.columns]
    df = df[keep]

    log.info("NUCC: %s taxonomy codes loaded", f"{len(df):,}")
    return df


# ───────────────────────────────────────────────────────────────────────────
# 2.  Benchmark workbook  (three sheets)
# ───────────────────────────────────────────────────────────────────────────

def load_benchmark_taxonomy_xwalk(cfg: dict) -> pd.DataFrame:
    """
    Sheet 'Taxonomy-Spec Xwalk': maps taxonomy codes to HiLabs specialty codes.

    Returns columns: spec_type, spec_cd, spec_descr, taxonomy_code
    """
    path = _benchmark_path(cfg)
    df = pd.read_excel(path, sheet_name="Taxonomy-Spec Xwalk", dtype=str).fillna("")
    df.columns = ["spec_type", "spec_cd", "spec_descr", "taxonomy_code"]
    log.info("Benchmark Taxonomy-Spec Xwalk: %s rows", f"{len(df):,}")
    return df


def load_benchmark_specialty_weights(cfg: dict) -> pd.DataFrame:
    """
    Sheet 'Specialty Weights': how important each specialty is.

    Returns columns: spec_cd, specialty, specialty_weight
    """
    path = _benchmark_path(cfg)
    df = pd.read_excel(path, sheet_name="Specialty Weights")
    df.columns = ["spec_cd", "specialty", "specialty_weight"]
    df["spec_cd"] = df["spec_cd"].astype(str)
    log.info("Benchmark Specialty Weights: %s rows", f"{len(df):,}")
    return df


def load_benchmark_codes(cfg: dict) -> pd.DataFrame:
    """
    Sheet 'Spec-Proc Weights': billing codes (PROC_CD) ranked per specialty.

    Returns columns: spec_cd, spec_description, billing_code,
                     spec_proc_rank, spec_proc_weight
    """
    path = _benchmark_path(cfg)
    df = pd.read_excel(path, sheet_name="Spec-Proc Weights")
    df.columns = ["spec_cd", "spec_description", "billing_code",
                   "spec_proc_rank", "spec_proc_weight"]
    df["spec_cd"] = df["spec_cd"].astype(str)
    df["billing_code"] = df["billing_code"].astype(str)
    log.info("Benchmark codes: %s rows, %s unique billing codes",
             f"{len(df):,}", f"{df['billing_code'].nunique():,}")
    return df


def _benchmark_path(cfg: dict) -> Path:
    ref_dir: Path = cfg["reference_dir"]
    name = cfg.get("c3", {}).get("benchmark_xlsx", "")
    if name:
        # config might store the name with underscores; the actual file has spaces
        # try exact match first, then glob
        exact = ref_dir / name
        if exact.exists():
            return exact
    return _find_file(ref_dir, "*Benchmarking*Hackathon*.xlsx")


# ───────────────────────────────────────────────────────────────────────────
# 3.  CMS DAC  (Doctors and Clinicians)
# ───────────────────────────────────────────────────────────────────────────

_DAC_USECOLS = [
    "NPI", "Provider Last Name", "Provider First Name", "Cred",
    "pri_spec", "sec_spec_all",
    "Facility Name", "org_pac_id",
    "adr_ln_1", "adr_ln_2", "City/Town", "State", "ZIP Code",
    "Telephone Number",
]

_DAC_RENAME = {
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


def load_dac(cfg: dict, *, state: str = "CA", force: bool = False) -> pd.DataFrame:
    """
    Load CMS DAC, filtered to *state* (default CA).

    The raw CSV is ~230 MB; the CA-only parquet cache is ~5–10 MB.
    """
    ref_dir: Path = cfg["reference_dir"]
    cache = ref_dir / f"dac_{state.lower()}.parquet"

    def _build():
        path = _find_file(ref_dir, "DAC_*ownloadable*.csv")
        log.info("Streaming DAC from %s  (filtering to state=%s) …", path.name, state)
        chunks = pd.read_csv(
            path,
            usecols=_DAC_USECOLS,
            dtype=str,
            chunksize=200_000,
        )
        parts = []
        for chunk in chunks:
            chunk = chunk[chunk["State"] == state]
            parts.append(chunk)
        df = pd.concat(parts, ignore_index=True)
        df = df.rename(columns=_DAC_RENAME)
        df["npi"] = pd.to_numeric(df["npi"], errors="coerce").astype("Int64")
        df["zip"] = df["zip"].str[:5]       # keep only 5-digit zip
        df = df.fillna("")
        return df

    df = _cached(cache, _build, force=force)
    log.info("DAC (%s): %s rows, %s unique NPIs", state, f"{len(df):,}", f"{df['npi'].nunique():,}")
    return df


# ───────────────────────────────────────────────────────────────────────────
# 4.  NPPES  npidata  (main NPI registry)
# ───────────────────────────────────────────────────────────────────────────

_NPPES_USECOLS = [
    "NPI",
    "Entity Type Code",
    "Employer Identification Number (EIN)",
    "Provider Organization Name (Legal Business Name)",
    "Provider Last Name (Legal Name)",
    "Provider First Name",
    "Provider Credential Text",
    "Provider First Line Business Practice Location Address",
    "Provider Second Line Business Practice Location Address",
    "Provider Business Practice Location Address City Name",
    "Provider Business Practice Location Address State Name",
    "Provider Business Practice Location Address Postal Code",
    "Provider Business Practice Location Address Telephone Number",
    "Provider Enumeration Date",
    "Last Update Date",
    "NPI Deactivation Date",
    "NPI Reactivation Date",
    "Healthcare Provider Taxonomy Code_1",
    "Healthcare Provider Primary Taxonomy Switch_1",
]

_NPPES_RENAME = {
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


def load_nppes(cfg: dict, *, state: str = "CA", force: bool = False) -> pd.DataFrame:
    """
    Load NPPES npidata, filtered to *state* practice location and active
    (no deactivation date, or has a reactivation date).

    The raw CSV is ~11 GB; the CA-only parquet cache is ~40–60 MB.
    Only the ~20 columns we need are read (out of 330).
    """
    nppes_dir: Path = cfg["nppes_dir"]
    cache = nppes_dir / f"nppes_{state.lower()}.parquet"

    def _build():
        path = _find_file(nppes_dir, "npidata_pfile_*.csv")
        log.info("Streaming NPPES npidata from %s  (filtering to state=%s) — this may take a few minutes …", path.name, state)
        chunks = pd.read_csv(
            path,
            usecols=_NPPES_USECOLS,
            dtype=str,
            chunksize=500_000,
            low_memory=False,
        )
        parts = []
        rows_total = 0
        for i, chunk in enumerate(chunks):
            rows_total += len(chunk)
            # Filter to state
            chunk = chunk[
                chunk["Provider Business Practice Location Address State Name"] == state
            ]
            # Filter to active: no deactivation, or reactivated
            deactivated = chunk["NPI Deactivation Date"].notna() & (chunk["NPI Deactivation Date"] != "")
            reactivated = chunk["NPI Reactivation Date"].notna() & (chunk["NPI Reactivation Date"] != "")
            chunk = chunk[~deactivated | reactivated]
            parts.append(chunk)
            if (i + 1) % 10 == 0:
                log.info("  … %s rows scanned, %s CA rows so far",
                         f"{rows_total:,}", f"{sum(len(p) for p in parts):,}")
        df = pd.concat(parts, ignore_index=True)
        df = df.rename(columns=_NPPES_RENAME)
        df["npi"] = pd.to_numeric(df["npi"], errors="coerce").astype("Int64")
        df["zip"] = df["zip"].str[:5]
        # Parse dates
        for col in ("last_update_date", "deactivation_date", "reactivation_date", "enumeration_date"):
            df[col] = pd.to_datetime(df[col], errors="coerce")
        df = df.fillna({c: "" for c in df.select_dtypes("object").columns})
        return df

    df = _cached(cache, _build, force=force)
    log.info("NPPES (%s): %s rows, %s unique NPIs, entity types: %s",
             state, f"{len(df):,}", f"{df['npi'].nunique():,}",
             df["entity_type"].value_counts().to_dict() if "entity_type" in df.columns else "?")
    return df


# ───────────────────────────────────────────────────────────────────────────
# 5.  NPPES  pl_pfile  (practice locations — secondary addresses)
# ───────────────────────────────────────────────────────────────────────────

_PL_RENAME = {
    "NPI": "npi",
    "Provider Secondary Practice Location Address- Address Line 1": "address_line1",
    "Provider Secondary Practice Location Address-  Address Line 2": "address_line2",
    "Provider Secondary Practice Location Address - City Name": "city",
    "Provider Secondary Practice Location Address - State Name": "state",
    "Provider Secondary Practice Location Address - Postal Code": "zip",
    "Provider Secondary Practice Location Address - Telephone Number": "phone",
}


def load_pl(cfg: dict, *, state: str = "CA", force: bool = False) -> pd.DataFrame:
    """
    Load NPPES practice-locations file (secondary addresses), filtered to *state*.

    The raw CSV is ~120 MB; the CA-only parquet cache is ~5–10 MB.
    """
    nppes_dir: Path = cfg["nppes_dir"]
    cache = nppes_dir / f"pl_{state.lower()}.parquet"

    def _build():
        path = _find_file(nppes_dir, "pl_pfile_*.csv")
        log.info("Streaming NPPES pl_pfile from %s  (filtering to state=%s) …", path.name, state)
        usecols = list(_PL_RENAME.keys())
        chunks = pd.read_csv(
            path,
            usecols=usecols,
            dtype=str,
            chunksize=200_000,
        )
        parts = []
        for chunk in chunks:
            chunk = chunk[
                chunk["Provider Secondary Practice Location Address - State Name"] == state
            ]
            parts.append(chunk)
        df = pd.concat(parts, ignore_index=True)
        df = df.rename(columns=_PL_RENAME)
        df["npi"] = pd.to_numeric(df["npi"], errors="coerce").astype("Int64")
        df["zip"] = df["zip"].str[:5]
        df = df.fillna("")
        return df

    df = _cached(cache, _build, force=force)
    log.info("PL (%s): %s rows, %s unique NPIs", state, f"{len(df):,}", f"{df['npi'].nunique():,}")
    return df


# ───────────────────────────────────────────────────────────────────────────
# Convenience: load everything at once
# ───────────────────────────────────────────────────────────────────────────

def load_all(cfg: dict) -> dict[str, pd.DataFrame]:
    """Return a dict of all reference frames, keyed by short name."""
    return {
        "nucc": load_nucc(cfg),
        "benchmark_codes": load_benchmark_codes(cfg),
        "benchmark_spec_weights": load_benchmark_specialty_weights(cfg),
        "benchmark_tax_xwalk": load_benchmark_taxonomy_xwalk(cfg),
        "dac": load_dac(cfg),
        "nppes": load_nppes(cfg),
        "pl": load_pl(cfg),
    }
