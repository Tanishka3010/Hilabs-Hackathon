from datetime import date

import pandas as pd
import pytest

from src import c5_provider_location as c5m
from src.common import load_config

C5 = load_config()["c5"]
AS_OF = date(2026, 10, 6)


@pytest.mark.parametrize("a,b", [
    (("123 Main Street", "Suite 400", "94107"), ("123 MAIN ST STE 400", "", "94107-1234")),
    (("22 Corporate Plaza", "#150", "92660"), ("22 CORPORATE PLZ", "", "92660")),
    (("1081 North China Lake Boulevard", "", "93555"), ("1081 N CHINA LAKE BLVD", "Bldg 2", "93555")),
])
def test_same_address_written_differently_gets_same_key(a, b):
    assert c5m.address_key(*a) == c5m.address_key(*b) != ""


def test_different_zip_or_street_is_different_address():
    assert c5m.address_key("123 Main St", "", "94107") != c5m.address_key("123 Main St", "", "94108")
    assert c5m.address_key("123 Main St", "", "94107") != c5m.address_key("125 Main St", "", "94107")
    assert c5m.address_key("", "", "94107") == ""


def test_unit_is_kept_separately():
    assert c5m.unit_of("1700 N ROSE AVE STE 320") == "STE 320"


@pytest.mark.parametrize("a,b,expected_min", [
    ("HOWARD FEIN MD INC", "Howard Fein, M.D., Inc.", 1.0),
    ("PPO: Sutter Medical Group", "SUTTER MEDICAL GROUP", 1.0),
    ("ALTA LOS ANGELES HOSPITALS, INC.", "Kaiser Foundation Hospitals", 0.0),
])
def test_name_similarity(a, b, expected_min):
    s = c5m.name_similarity(a, b)
    assert s >= expected_min if expected_min else s < 0.5


@pytest.mark.parametrize("update,expected", [("2026-06-01", 1.0), ("2024-06-01", 0.7), ("2019-01-01", 0.4), (None, 0.4)])
def test_recency_factor(update, expected):
    assert c5m.recency_factor(pd.Timestamp(update) if update else None, AS_OF, C5) == expected


def nppes_frame():
    return pd.DataFrame([
        {"npi": 1, "entity_type": "1", "org_name": "", "first_name": "ANN", "last_name": "LEE", "address_line1": "123 Main St",
         "address_line2": "Ste 4", "city": "SF", "state": "CA", "zip": "94107", "last_update_date": pd.Timestamp("2026-05-01"),
         "nppes_status": "active", "taxonomy_code": "207Q00000X"},
        {"npi": 2, "entity_type": "2", "org_name": "ACME CLINIC INC", "first_name": "", "last_name": "", "address_line1": "9 Oak Rd",
         "address_line2": "", "city": "LA", "state": "CA", "zip": "90001", "last_update_date": pd.Timestamp("2018-01-01"),
         "nppes_status": "active", "taxonomy_code": "261QP2300X"},
        {"npi": 3, "entity_type": "2", "org_name": "NATIONAL LAB", "first_name": "", "last_name": "", "address_line1": "1 Lab Way",
         "address_line2": "", "city": "Burlington", "state": "NC", "zip": "27215", "last_update_date": pd.Timestamp("2025-01-01"),
         "nppes_status": "active", "taxonomy_code": "291U00000X"},
        {"npi": 4, "entity_type": "1", "org_name": "", "first_name": "OLD", "last_name": "DOC", "address_line1": "", "address_line2": "",
         "city": "", "state": "", "zip": "", "last_update_date": pd.NaT, "nppes_status": "deactivated", "taxonomy_code": ""},
    ])


def dac_frame():
    return pd.DataFrame([
        {"npi": 1, "facility_name": "LEE FAMILY PRACTICE INC", "address_line1": "123 MAIN STREET", "address_line2": "SUITE 4", "city": "SAN FRANCISCO",
         "state": "CA", "zip": "941071234"},
        {"npi": 1, "facility_name": "BAY GROUP", "address_line1": "500 Pine St", "address_line2": "", "city": "SF", "state": "CA", "zip": "94108"},
    ])


@pytest.fixture
def built():
    nppes = nppes_frame()
    pl = pd.DataFrame([{"npi": 2, "address_line1": "77 Elm Ave", "address_line2": "", "city": "LA", "state": "CA", "zip": "90002"}])
    cand = c5m.build_candidates(nppes, pl, dac_frame())
    addr = c5m.score_addresses(cand, nppes, C5, AS_OF)
    prov = c5m.provider_summary({1, 2, 3, 4, 5}, addr, nppes, C5).set_index("npi")
    refs = pd.DataFrame([
        {"npi": 1, "tin": "11-1", "business_name": "Bay Group"},
        {"npi": 1, "tin": "11-2", "business_name": "Lee Family Practice"},
        {"npi": 2, "tin": "22-2", "business_name": "PPO: Acme Clinic"},
    ])
    links = c5m.link_tin_npi_address(refs, addr, nppes, C5)
    return cand, addr, prov, links


def test_agreeing_sources_verify_and_raise_confidence(built):
    _, addr, prov, _ = built
    best = addr[(addr["npi"] == 1) & (addr["rank"] == 1)].iloc[0]
    assert best["verified"] and best["sources"] == "dac|nppes_primary"
    assert best["confidence"] == pytest.approx(1 - (1 - 0.7) * (1 - 0.5))
    assert prov.loc[1, "location_status"] == "verified"


def test_single_nppes_source_is_not_verified_and_stale_is_down_weighted(built):
    _, addr, prov, _ = built
    a2 = addr[addr["npi"] == 2].set_index("address_line1")
    assert not a2["verified"].any()
    assert a2.loc["9 Oak Rd", "confidence"] == pytest.approx(0.5 * 0.4)
    assert prov.loc[2, "location_status"] == "unverified_multiple_candidates"


def test_statuses_out_of_state_deactivated_missing(built):
    _, _, prov, _ = built
    assert prov.loc[3, "location_status"] == "out_of_state_only"
    assert prov.loc[4, "location_status"] == "deactivated_npi"
    assert prov.loc[5, "location_status"] == "no_address" and prov.loc[5, "nppes_status"] == "not_in_nppes"


def test_tin_specific_address_comes_first(built):
    _, addr, _, links = built
    pine = addr[(addr["npi"] == 1) & addr["address_line1"].str.contains("Pine")].iloc[0]["address_id"]
    first_for_bay = links[(links["tin"] == "11-1") & (links["pair_rank"] == 1)].iloc[0]
    assert first_for_bay["address_id"] == pine and "dac_group_name_matches_tin" in first_for_bay["link_evidence"]
    first_for_lee = links[(links["tin"] == "11-2") & (links["pair_rank"] == 1)].iloc[0]
    assert first_for_lee["link_evidence"] == "dac_group_name_matches_tin+verified_address"
    acme = links[links["tin"] == "22-2"]
    assert acme["link_evidence"].str.contains("nppes_org_name_matches_tin").all()
