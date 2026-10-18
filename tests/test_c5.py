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


def test_tin_specific_address_comes_first_dac(built):
    _, addr, _, links = built
    pine = addr[(addr["npi"] == 1) & addr["address_line1"].str.contains("Pine")].iloc[0]["address_id"]
    first_for_bay = links[(links["tin"] == "11-1") & (links["pair_rank"] == 1)].iloc[0]
    assert first_for_bay["address_id"] == pine and "dac_group_name_matches_tin" in first_for_bay["link_evidence"]
    first_for_lee = links[(links["tin"] == "11-2") & (links["pair_rank"] == 1)].iloc[0]
    assert first_for_lee["link_evidence"] == "dac_group_name_matches_tin+verified_address"
    acme = links[links["tin"] == "22-2"]
    assert acme["link_evidence"].str.contains("nppes_org_name_matches_tin").all()


def facility_frame():
    return pd.DataFrame([
        {"npi": 2, "facility_name": "ACME CLINIC", "business_name": "ACME HOLDINGS", "address_line1": "9 OAK ROAD", "city": "LOS ANGELES",
         "county": "LOS ANGELES", "state": "CA", "zip": "90001", "data_date": pd.Timestamp("2026-10-16")},
        {"npi": None, "facility_name": "BAYSIDE SURGERY CENTER", "business_name": "", "address_line1": "1 Bay St", "city": "OAKLAND",
         "county": "ALAMEDA", "state": "CA", "zip": "94601", "data_date": pd.Timestamp("2026-10-16")},
        {"npi": None, "facility_name": "OAKLAND HEALTH CENTER", "business_name": "", "address_line1": "5 Lake St", "city": "OAKLAND",
         "county": "ALAMEDA", "state": "CA", "zip": "94601", "data_date": pd.Timestamp("2026-10-16")},
    ])


def test_registry_and_irs_sources_verify_and_link():
    nppes = pd.concat([nppes_frame(), pd.DataFrame([
        {"npi": 6, "entity_type": "2", "org_name": "BAYSIDE SURGERY CENTER LLC", "first_name": "", "last_name": "", "address_line1": "1 Bay Street",
         "address_line2": "", "city": "Oakland", "state": "CA", "zip": "94601", "last_update_date": pd.Timestamp("2015-01-01"),
         "nppes_status": "active", "taxonomy_code": "261QA1903X"},
        {"npi": 7, "entity_type": "2", "org_name": "OAKLAND DIALYSIS INC", "first_name": "", "last_name": "", "address_line1": "8 Pine St",
         "address_line2": "", "city": "Oakland", "state": "CA", "zip": "94601", "last_update_date": pd.Timestamp("2015-01-01"),
         "nppes_status": "active", "taxonomy_code": "261QE0700X"}])], ignore_index=True)
    pl = pd.DataFrame(columns=["npi", "address_line1", "address_line2", "city", "state", "zip"])
    refs = pd.DataFrame([{"npi": 2, "tin": "22-2222222", "business_name": "Acme"}, {"npi": 7, "tin": "77-7777777", "business_name": "Oakland Dialysis"}])
    irs = pd.DataFrame([{"ein": "777777777", "name": "OAKLAND DIALYSIS INC", "address_line1": "8 PINE STREET", "city": "OAKLAND", "state": "CA", "zip": "94601"}])
    fac = c5m.facility_candidates(facility_frame(), nppes, pl, {2, 6, 7}, C5)
    assert set(fac["source"]) == {"ca_facility", "ca_facility_name_zip"}
    named = fac[fac["source"] == "ca_facility_name_zip"]
    assert named["npi"].tolist() == [6] and named["source_name"].tolist() == ["BAYSIDE SURGERY CENTER"]
    cand = c5m.build_candidates(nppes, pl, pd.DataFrame(columns=["npi"]), [fac, c5m.irs_candidates(irs, refs)])
    addr = c5m.score_addresses(cand, nppes, C5, AS_OF)
    best = addr[addr["rank"] == 1].set_index("npi")
    assert best.loc[2, "verified"] and best.loc[2, "verification_basis"] == "two_independent_sources"
    assert best.loc[6, "verified"] and best.loc[6, "sources"] == "ca_facility_name_zip|nppes_primary"
    assert best.loc[7, "verified"] and "irs_eo" in best.loc[7, "sources"]
    links = c5m.link_tin_npi_address(refs, addr, nppes, C5)
    assert "irs_address_for_tin" in links[links["npi"] == 7].iloc[0]["link_evidence"]


def test_state_register_alone_verifies():
    nppes = nppes_frame().iloc[[1]].assign(address_line1="99 Other Rd", zip="90009")
    fac = c5m.facility_candidates(facility_frame().iloc[[0]], nppes, pd.DataFrame(columns=["npi", "zip"]), {2}, C5)
    addr = c5m.score_addresses(c5m.build_candidates(nppes, pd.DataFrame(), pd.DataFrame(), [fac]), nppes, C5, AS_OF)
    best = addr[addr["rank"] == 1].iloc[0]
    assert best["sources"] == "ca_facility" and best["verified"] and best["verification_basis"] == "state_license_register"


def test_place_names_do_not_make_a_name_match():
    drop = c5m.place_words("SANTA MARIA", "SANTA BARBARA")
    assert c5m.name_similarity("THE ENDOSCOPY CENTER OF SANTA MARIA LLC", "SANTA MARIA HEALTH CENTER", drop) == 0.0
    assert c5m.name_similarity("KAISER FOUNDATION HOSPITALS", "KAISER FOUNDATION HOSPITAL - FREMONT", c5m.place_words("FREMONT")) == 1.0


def test_dac_group_practice_confirms_organisation_address():
    nppes = pd.DataFrame([
        {"npi": 8, "entity_type": "2", "org_name": "RIPON FAMILY PHYSICIANS", "first_name": "", "last_name": "", "address_line1": "521 N Wilma Ave",
         "address_line2": "", "city": "Ripon", "state": "CA", "zip": "95366", "last_update_date": pd.Timestamp("2016-01-01"),
         "nppes_status": "active", "taxonomy_code": "261QP2300X"},
        {"npi": 9, "entity_type": "2", "org_name": "MODESTO HEART CENTER", "first_name": "", "last_name": "", "address_line1": "1 Main St",
         "address_line2": "", "city": "Modesto", "state": "CA", "zip": "95350", "last_update_date": pd.Timestamp("2016-01-01"),
         "nppes_status": "active", "taxonomy_code": "261QP2300X"},
    ])
    dac_state = pd.DataFrame([
        {"npi": 100, "facility_name": "RIPON FAMILY PHYSICIANS INC", "address_line1": "521 NORTH WILMA AVENUE", "address_line2": "", "city": "RIPON",
         "state": "CA", "zip": "953661234"},
        {"npi": 101, "facility_name": "RIPON FAMILY PHYSICIANS", "address_line1": "9 Far Rd", "address_line2": "", "city": "FRESNO", "state": "CA", "zip": "93701"},
        {"npi": 102, "facility_name": "MODESTO HEALTH CENTER", "address_line1": "1 MAIN ST", "address_line2": "", "city": "MODESTO", "state": "CA", "zip": "95350"},
    ])
    pl = pd.DataFrame(columns=["npi", "zip"])
    grp = c5m.dac_group_candidates(dac_state, nppes, pl, C5)
    assert grp["npi"].tolist() == [8] and grp["source"].tolist() == ["dac_group"]
    addr = c5m.score_addresses(c5m.build_candidates(nppes, pl, pd.DataFrame(), [grp]), nppes, C5, AS_OF)
    best = addr[addr["rank"] == 1].set_index("npi")
    assert best.loc[8, "verified"] and best.loc[8, "sources"] == "dac_group|nppes_primary"
    assert not best.loc[9, "verified"]


@pytest.mark.parametrize("a,b,ok", [
    ("SKYWAY SURGERY CENTER. LLC", "SKYWAY SURGERY CENTER LLC", True),
    ("AURORA MEDICAL CORPORATION", "AURORA MEDICAL CENTER", False),
    ("OAKLAND SURGERY CENTER LLC", "OAKLAND SURGERY CENTER", False),
    ("ROBERT HARTMAN", "ROBERT HARTMAN MD", True),
    ("WEI WANG", "WANG DENTAL", False),
])
def test_strong_name_match_needs_two_shared_words(a, b, ok):
    assert c5m.strong_name_match(a, b, c5m.place_words("OAKLAND"), 0.6) is ok


def test_clia_sites_become_candidates_for_people_and_organisations():
    nppes = nppes_frame()
    clia = pd.DataFrame([
        {"clia_id": "05D1", "facility_name": "ANN LEE MD", "address_line1": "123 MAIN ST", "address_line2": "STE 4", "city": "SAN FRANCISCO", "state": "CA", "zip": "94107"},
        {"clia_id": "05D2", "facility_name": "ACME CLINIC INC", "address_line1": "9 OAK RD", "address_line2": "", "city": "LOS ANGELES", "state": "CA", "zip": "90001"},
        {"clia_id": "05D3", "facility_name": "ACME", "address_line1": "1 Other St", "address_line2": "", "city": "LOS ANGELES", "state": "CA", "zip": "90001"},
    ])
    cand = c5m.clia_candidates(clia, nppes, pd.DataFrame(columns=["npi", "zip"]), C5)
    assert sorted(zip(cand["npi"], cand["source_name"])) == [(1, "ANN LEE MD"), (2, "ACME CLINIC INC")]
    addr = c5m.score_addresses(c5m.build_candidates(nppes, pd.DataFrame(), pd.DataFrame(), [cand]), nppes, C5, AS_OF)
    best = addr[addr["rank"] == 1].set_index("npi")
    assert best.loc[2, "verified"] and best.loc[2, "sources"] == "cms_clia|nppes_primary"
