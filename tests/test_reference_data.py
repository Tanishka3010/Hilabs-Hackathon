"""
Tests for src/reference_data.py — Person B owns this file.

These tests run against the real reference files that should be present
in the repository before the test suite is run.  Slow tests (NPPES npidata
stream) are marked so they can be skipped in quick local runs.
"""

import pytest
import pandas as pd

from src.common import load_config
from src import reference_data as rd


@pytest.fixture(scope="module")
def cfg():
    return load_config()


# ───────────────────────────────────────────────────────────────────────
# NUCC
# ───────────────────────────────────────────────────────────────────────

class TestNUCC:
    def test_loads_and_has_key_columns(self, cfg):
        df = rd.load_nucc(cfg)
        assert len(df) > 0
        assert "taxonomy_code" in df.columns
        assert "classification" in df.columns
        assert "display_name" in df.columns

    def test_taxonomy_codes_are_unique(self, cfg):
        df = rd.load_nucc(cfg)
        assert df["taxonomy_code"].is_unique, "duplicate taxonomy codes"


# ───────────────────────────────────────────────────────────────────────
# Benchmark workbook
# ───────────────────────────────────────────────────────────────────────

class TestBenchmark:
    def test_codes_has_billing_code(self, cfg):
        df = rd.load_benchmark_codes(cfg)
        assert len(df) > 0
        assert "billing_code" in df.columns
        assert "spec_cd" in df.columns

    def test_spec_weights_has_weight_column(self, cfg):
        df = rd.load_benchmark_specialty_weights(cfg)
        assert "specialty_weight" in df.columns
        assert df["specialty_weight"].dtype in (float, int, "int64", "float64")

    def test_taxonomy_xwalk_has_taxonomy_code(self, cfg):
        df = rd.load_benchmark_taxonomy_xwalk(cfg)
        assert "taxonomy_code" in df.columns
        assert "spec_cd" in df.columns

    def test_taxonomy_xwalk_codes_match_nucc(self, cfg):
        """Benchmark taxonomy codes should be a subset of NUCC codes."""
        nucc = rd.load_nucc(cfg)
        xwalk = rd.load_benchmark_taxonomy_xwalk(cfg)
        nucc_codes = set(nucc["taxonomy_code"])
        xwalk_codes = set(xwalk["taxonomy_code"])
        # Allow a small mismatch (some codes may be deprecated)
        overlap = xwalk_codes & nucc_codes
        pct = len(overlap) / len(xwalk_codes) if xwalk_codes else 0
        assert pct > 0.90, f"only {pct:.0%} of xwalk codes found in NUCC"


# ───────────────────────────────────────────────────────────────────────
# DAC
# ───────────────────────────────────────────────────────────────────────

class TestDAC:
    def test_loads_ca_only(self, cfg):
        df = rd.load_dac(cfg)
        assert len(df) > 0
        assert (df["state"] == "CA").all(), "non-CA rows leaked through"

    def test_has_address_columns(self, cfg):
        df = rd.load_dac(cfg)
        for col in ("npi", "address_line1", "city", "state", "zip"):
            assert col in df.columns, f"missing column: {col}"

    def test_npi_is_int64(self, cfg):
        df = rd.load_dac(cfg)
        assert str(df["npi"].dtype) == "Int64"


# ───────────────────────────────────────────────────────────────────────
# NPPES npidata  (slow — requires streaming 11 GB)
# ───────────────────────────────────────────────────────────────────────

class TestNPPES:
    @pytest.mark.slow
    def test_loads_ca_only_and_active(self, cfg):
        df = rd.load_nppes(cfg)
        assert len(df) > 0
        assert (df["state"] == "CA").all(), "non-CA rows leaked through"
        # Every row should be active (no deactivation, or reactivated)
        has_deactivation = df["deactivation_date"].notna()
        has_reactivation = df["reactivation_date"].notna()
        assert (~has_deactivation | has_reactivation).all(), \
            "deactivated (non-reactivated) rows leaked through"

    @pytest.mark.slow
    def test_cache_is_created(self, cfg):
        """After first load, a parquet cache should exist."""
        cache = cfg["nppes_dir"] / "nppes_ca.parquet"
        _ = rd.load_nppes(cfg)
        assert cache.exists(), "parquet cache was not written"

    @pytest.mark.slow
    def test_has_taxonomy_code(self, cfg):
        df = rd.load_nppes(cfg)
        assert "taxonomy_code" in df.columns


# ───────────────────────────────────────────────────────────────────────
# NPPES pl_pfile  (practice locations)
# ───────────────────────────────────────────────────────────────────────

class TestPL:
    @pytest.mark.slow
    def test_loads_ca_only(self, cfg):
        df = rd.load_pl(cfg)
        assert len(df) > 0
        assert (df["state"] == "CA").all(), "non-CA rows leaked through"

    @pytest.mark.slow
    def test_has_address_columns(self, cfg):
        df = rd.load_pl(cfg)
        for col in ("npi", "address_line1", "city", "zip"):
            assert col in df.columns


# ───────────────────────────────────────────────────────────────────────
# load_all convenience
# ───────────────────────────────────────────────────────────────────────

class TestLoadAll:
    @pytest.mark.slow
    def test_returns_all_keys(self, cfg):
        data = rd.load_all(cfg)
        expected = {"nucc", "benchmark_codes", "benchmark_spec_weights",
                    "benchmark_tax_xwalk", "dac", "nppes", "pl"}
        assert set(data.keys()) == expected
