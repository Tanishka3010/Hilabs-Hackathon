import json
from datetime import date

import pandas as pd
import pytest

from src import c4_extract_rates as c4
from src import ptd_reader
from src.common import load_config

FILE_KEY = "testfile0000004"


def price(rate, typ="negotiated", exp="9999-12-31", cls="professional"):
    return {"negotiated_type": typ, "negotiated_rate": rate, "expiration_date": exp, "service_code": ["11"], "billing_class": cls}


def item(code, name, rates, refs=(1,)):
    return {"negotiation_arrangement": "ffs", "name": name, "billing_code_type": "CPT", "billing_code": code,
            "negotiated_rates": [{"provider_references": list(refs), "negotiated_prices": [price(*r) if isinstance(r, tuple) else price(r) for r in rates]}]}


IN_NETWORK = {
    "reporting_entity_name": "Test", "reporting_entity_type": "TPA", "version": "2.0.0", "last_updated_on": "2026-09-01",
    "provider_references": [
        {"provider_group_id": 1, "provider_groups": [{"npi": [1234567893], "tin": {"type": "ein", "value": "11-1111111"}}]},
        {"provider_group_id": 2, "provider_groups": [{"npi": [1770688129], "tin": {"type": "ein", "value": "22-2222222", "business_name": "PPO: Los Angeles County"}}]},
    ],
    "in_network": [
        item("10001", "mode", [100, 100, 150]),
        item("10002", "tie two", [10, 20]),
        item("10003", "tie three", [10, 20, 30]),
        item("10004", "type preference", [(40, "derived"), (50, "negotiated")]),
        item("10005", "outlier", [100, 5000]),
        item("10005", "outlier", [90, 95, 100, 105, 110], refs=(2,)),
        item("10006", "drops", [0, (70, "negotiated", "2020-01-01"), (5, "percentage"), 80]),
        item("10007", "all invalid", [0]),
    ],
}


@pytest.fixture
def selected(tmp_path):
    out = tmp_path / "out"
    src = tmp_path / "in.json"
    src.write_text(json.dumps(IN_NETWORK))
    ptd_reader.extract_local(FILE_KEY, "https://example.com/2026-09-01_test_in-network.json", src, out / "c4")
    cfg = load_config(None, {"output_dir": str(out)})
    cfg["c3"] = {**cfg["c3"], "outlier_min_group": 3}
    stats = c4.select_rates(cfg, out / "c4" / FILE_KEY, date(2026, 9, 1))
    df = pd.read_parquet(out / "c4" / FILE_KEY / "rates_selected.parquet")
    return stats, df


def pick(df, code, npi=1234567893):
    rows = df[(df["billing_code"] == code) & (df["npi"] == npi)]
    assert len(rows) == 1
    return rows.iloc[0]


def test_most_frequent_rate_wins(selected):
    r = pick(selected[1], "10001")
    assert r.negotiated_rate == 100 and r.selection_rule == "most_frequent_rate" and r.has_conflict
    assert (r.rate_min, r.rate_max, r.n_candidate_rows) == (100, 150, 3)


@pytest.mark.parametrize("code,expected", [("10002", 10), ("10003", 20)])
def test_tied_rates_take_lower_median(selected, code, expected):
    r = pick(selected[1], code)
    assert r.negotiated_rate == expected and r.selection_rule == "lower_median_of_tied_rates"


def test_rate_type_preference(selected):
    r = pick(selected[1], "10004")
    assert r.negotiated_rate == 50 and r.negotiated_type == "negotiated"
    assert r.selection_rule.startswith("rate_type_preference")


def test_outlier_is_set_aside_when_alternative_exists(selected):
    r = pick(selected[1], "10005")
    assert r.negotiated_rate == 100 and "outliers_set_aside" in r.selection_rule and r.rate_max == 5000


def test_unusable_rates_are_dropped_with_reasons(selected):
    stats, df = selected
    r = pick(df, "10006")
    assert r.negotiated_rate == 80 and r.selection_rule == "single_rate"
    assert stats["dropped_rows"] == {"invalid_rate": 2, "expired": 1, "non_dollar_rate_type": 1}
    assert df[df["billing_code"] == "10007"].empty


def test_one_row_per_context_and_network_hint(selected):
    stats, df = selected
    assert stats["selected_rows"] == len(df) == 7
    other = pick(df, "10005", npi=1770688129)
    assert other.network_hint == "PPO" and other.selection_rule == "lower_median_of_tied_rates" and other.negotiated_rate == 100
