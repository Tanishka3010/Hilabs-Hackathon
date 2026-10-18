import pandas as pd
import pytest

from src.common import load_config
from src.contracts import FILE_HEADER_KEYS, RATES_LONG_COLUMNS, conform, empty_frame, validate_header


def sample_rates_long():
    return pd.DataFrame([
        {"file_key": "k1", "in_network_idx": 0, "negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code_type_version": "2026",
         "billing_code": "99213", "name": "Office visit", "description": "", "rate_idx": 0, "price_idx": 0, "provider_group_id": "1", "provider_ref_resolved": True,
         "npi": 1770688129, "tin_type": "ein", "tin": "68-0273974", "negotiated_type": "negotiated", "negotiated_rate": 95.0,
         "expiration_date": "9999-12-31", "billing_class": "professional", "service_code": "11", "billing_code_modifier": "", "additional_information": ""},
    ])


def test_conform_keeps_contract_order_and_types():
    df = conform(sample_rates_long(), RATES_LONG_COLUMNS)
    assert list(df.columns) == list(RATES_LONG_COLUMNS)
    assert str(df["npi"].dtype) == "Int64"


def test_conform_rejects_missing_columns():
    with pytest.raises(ValueError, match="missing contract columns"):
        conform(sample_rates_long().drop(columns=["tin"]), RATES_LONG_COLUMNS)


def test_empty_frame_and_header():
    assert list(empty_frame(RATES_LONG_COLUMNS).columns) == list(RATES_LONG_COLUMNS)
    with pytest.raises(ValueError):
        validate_header({"file_key": "k1"})
    assert validate_header({k: None for k in FILE_HEADER_KEYS})


def test_stage_configs_are_merged():
    cfg = load_config()
    assert {"c2", "c3", "c4", "c5"} <= set(cfg)
    assert cfg["ptd_dir"].name == "ptd" and cfg["nppes_dir"].parent.name == "raw"
