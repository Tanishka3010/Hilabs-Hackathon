import pandas as pd
import pytest

from src import reference_data as rd
from src.common import ROOT, load_config

REAL_REF = ROOT / "reference"
HAS_BENCHMARK = bool(sorted(REAL_REF.glob("*Benchmarking*Hackathon*.xlsx")))
HAS_NUCC = bool(sorted(REAL_REF.glob("nucc_taxonomy*.csv")))

NPI_CA_ACTIVE = 1234567893
NPI_NV_ACTIVE = 1770688129
NPI_CA_DEACT = 1063646446
NPI_REACT = 1285009035
NPI_OTHER = 1902520455


def nppes_row(npi, state, deact="", react="", updated="01/15/2020"):
    row = {k: "" for k in rd.NPPES_RENAME}
    row.update({"NPI": str(npi), "Entity Type Code": "1", "Provider Business Practice Location Address State Name": state,
                "Provider Business Practice Location Address Postal Code": "941071234", "Last Update Date": updated,
                "NPI Deactivation Date": deact, "NPI Reactivation Date": react, "Healthcare Provider Taxonomy Code_1": "207Q00000X",
                "Unused Column": "x"})
    return row


@pytest.fixture
def cfg(tmp_path):
    nppes, ref, out = tmp_path / "nppes", tmp_path / "ref", tmp_path / "out"
    nppes.mkdir()
    ref.mkdir()
    pd.DataFrame([
        nppes_row(NPI_CA_ACTIVE, "CA"),
        nppes_row(NPI_NV_ACTIVE, "NV"),
        nppes_row(NPI_CA_DEACT, "CA", deact="03/01/2021"),
        nppes_row(NPI_REACT, "ca", deact="03/01/2021", react="06/01/2022"),
        nppes_row(NPI_OTHER, "TX"),
    ]).to_csv(nppes / "npidata_pfile_20050523-20260907.csv", index=False)
    pd.DataFrame([{k: "" for k in rd.NPPES_RENAME}]).to_csv(nppes / "npidata_pfile_20050523-20260907_fileheader.csv", index=False)
    pl = [{k: "" for k in rd.PL_RENAME} for _ in range(2)]
    pl[0].update({"NPI": str(NPI_NV_ACTIVE), "Provider Secondary Practice Location Address - State Name": "CA",
                  "Provider Secondary Practice Location Address - Postal Code": "90001"})
    pl[1].update({"NPI": str(NPI_OTHER), "Provider Secondary Practice Location Address - State Name": "TX"})
    pd.DataFrame(pl).to_csv(nppes / "pl_pfile_20050523-20260907.csv", index=False)
    dac = [{k: "" for k in rd.DAC_RENAME} for _ in range(3)]
    dac[0].update({"NPI": str(NPI_CA_ACTIVE), "State": "CA", "ZIP Code": "941071234"})
    dac[1].update({"NPI": str(NPI_CA_ACTIVE), "State": "CA", "ZIP Code": "94110"})
    dac[2].update({"NPI": str(NPI_NV_ACTIVE), "State": "NV", "ZIP Code": "89101"})
    pd.DataFrame(dac).to_csv(ref / "DAC_NationalDownloadableFile.csv", index=False)
    c = load_config(None, {"nppes_dir": str(nppes), "reference_dir": str(ref), "output_dir": str(out)})
    c["reference_data"] = {"fallback_states": ["CA"]}
    return c


TARGET = {NPI_CA_ACTIVE, NPI_NV_ACTIVE, NPI_CA_DEACT, NPI_REACT, 1111111112}


@pytest.mark.parametrize("npi,ok", [
    (1234567893, True), ("1770688129", True), (1234567890, False), ("123456789", False),
    ("12345678901", False), ("abcdefghij", False), (None, False), (3234567891, False),
])
def test_npi_is_valid(npi, ok):
    assert rd.npi_is_valid(npi) is ok


def test_nppes_scoped_by_npis_keeps_out_of_state_and_deactivated(cfg):
    df = rd.load_nppes(cfg, npis=TARGET).set_index("npi")
    assert set(df.index) == {NPI_CA_ACTIVE, NPI_NV_ACTIVE, NPI_CA_DEACT, NPI_REACT}
    assert df.loc[NPI_NV_ACTIVE, "state"] == "NV"
    assert df.loc[NPI_CA_DEACT, "nppes_status"] == "deactivated"
    assert df.loc[NPI_REACT, "nppes_status"] == "reactivated"
    assert df.loc[NPI_REACT, "state"] == "CA"
    assert df.loc[NPI_CA_ACTIVE, "nppes_status"] == "active"
    assert df.loc[NPI_CA_ACTIVE, "zip"] == "94107"
    assert str(df.loc[NPI_CA_ACTIVE, "last_update_date"].date()) == "2020-01-15"


def test_nppes_cache_is_reused_without_raw_file(cfg):
    first = rd.load_nppes(cfg, npis=TARGET)
    for p in cfg["nppes_dir"].glob("npidata_pfile_*.csv"):
        p.unlink()
    second = rd.load_nppes(cfg, npis=TARGET)
    assert len(first) == len(second) == 4
    assert list((cfg["output_dir"] / "_cache" / "reference").glob("nppes_npis-*.parquet"))


def test_nppes_state_fallback_when_no_provider_refs(cfg):
    df = rd.load_nppes(cfg)
    assert set(df["npi"]) == {NPI_CA_ACTIVE, NPI_CA_DEACT, NPI_REACT}


def test_scope_comes_from_c4_provider_refs(cfg):
    d = cfg["output_dir"] / "c4" / "abc123"
    d.mkdir(parents=True)
    pd.DataFrame({"npi": pd.array([NPI_NV_ACTIVE, NPI_OTHER, None], dtype="Int64")}).to_parquet(d / "provider_refs.parquet")
    assert rd.target_npis(cfg) == {NPI_NV_ACTIVE, NPI_OTHER}
    assert set(rd.load_nppes(cfg)["npi"]) == {NPI_NV_ACTIVE, NPI_OTHER}


def test_pl_scoped_by_npis(cfg):
    df = rd.load_pl(cfg, npis={NPI_NV_ACTIVE})
    assert df["npi"].tolist() == [NPI_NV_ACTIVE]
    assert df["state"].tolist() == ["CA"]


def test_dac_national_file_keeps_all_states_and_all_addresses(cfg):
    df = rd.load_dac(cfg, npis={NPI_CA_ACTIVE, NPI_NV_ACTIVE})
    assert sorted(df["state"]) == ["CA", "CA", "NV"]
    assert set(df["dac_source"]) == {"national_file"}
    assert set(df["zip"]) == {"94107", "94110", "89101"}


def test_dac_falls_back_to_legacy_ca_cache(cfg):
    ref = cfg["reference_dir"]
    (ref / "DAC_NationalDownloadableFile.csv").unlink()
    pd.DataFrame({"npi": pd.array([NPI_CA_ACTIVE], dtype="Int64"), "state": ["CA"], "zip": ["94107"]}).to_parquet(ref / "dac_ca.parquet")
    df = rd.load_dac(cfg, npis={NPI_CA_ACTIVE, NPI_NV_ACTIVE})
    assert df["npi"].tolist() == [NPI_CA_ACTIVE]
    assert set(df["dac_source"]) == {"legacy_ca_only"}


def test_dac_missing_everywhere_raises(cfg):
    (cfg["reference_dir"] / "DAC_NationalDownloadableFile.csv").unlink()
    with pytest.raises(FileNotFoundError):
        rd.load_dac(cfg, npis={NPI_CA_ACTIVE})


@pytest.mark.skipif(not HAS_NUCC, reason="reference/nucc_taxonomy*.csv not present")
def test_real_nucc_codes_unique():
    df = rd.load_nucc(load_config())
    assert len(df) > 800 and df["taxonomy_code"].is_unique


@pytest.mark.skipif(not HAS_BENCHMARK, reason="benchmark xlsx not present")
def test_real_benchmark_sheets():
    cfg = load_config()
    codes = rd.load_benchmark_codes(cfg)
    assert {"spec_cd", "billing_code", "spec_proc_weight"} <= set(codes.columns)
    assert codes["billing_code"].str.len().eq(5).all()
    assert "00170" in set(codes["billing_code"])
    weights = rd.load_benchmark_specialty_weights(cfg)
    assert pd.api.types.is_numeric_dtype(weights["specialty_weight"])
    xwalk = rd.load_benchmark_taxonomy_xwalk(cfg)
    if HAS_NUCC:
        nucc = set(rd.load_nucc(cfg)["taxonomy_code"])
        assert len(set(xwalk["taxonomy_code"]) & nucc) / xwalk["taxonomy_code"].nunique() > 0.9
