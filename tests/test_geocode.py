import pandas as pd
import pytest

from src import c5_provider_location as c5m
from src import geocode as geo

CENSUS_TEXT = (
    '"7901 FROST ST|92123","7901 FROST ST, SAN DIEGO, CA, 92123","Match","Exact","7901 FROST ST, SAN DIEGO, CA, 92123",'
    '"-117.155,32.800","657459747","R","06","073","008702","1003"\n'
    '"22 CORPORATE PLZ|92660","22 CORPORATE PLZ, NEWPORT BEACH, CA, 92660","No_Match"\n'
)


def test_parse_census_batch_response():
    df = geo.parse_census(CENSUS_TEXT).set_index("address_key")
    assert df.loc["7901 FROST ST|92123", "geo_status"] == "Match"
    assert (df.loc["7901 FROST ST|92123", "county_fips"], df.loc["7901 FROST ST|92123", "latitude"]) == ("073", "32.800")
    assert df.loc["22 CORPORATE PLZ|92660", "geo_status"] == "No_Match"


def test_geocode_caches_and_only_sends_new_addresses(tmp_path):
    calls = []

    def fake(rows, settings):
        calls.append([r[0] for r in rows])
        return "\n".join(f'"{r[0]}","x","Match","Exact","{r[1]}, {r[2]}, {r[3]}, {r[4]}","-1,2","1","L","06","001","000100","1000"' for r in rows)

    addrs = pd.DataFrame({"address_key": ["1 A ST|90001", "2 B ST|90002"], "street": ["1 A ST", "2 B ST"], "city": ["X", "Y"], "state": ["CA", "CA"], "zip": ["90001", "90002"]})
    cache = tmp_path / "geo.parquet"
    first = geo.geocode(addrs, cache, {"batch_size": 1, "workers": 2}, fetch=fake)
    assert set(first["geo_status"]) == {"Match"} and len(calls) == 2
    more = pd.concat([addrs, pd.DataFrame({"address_key": ["3 C ST|90003"], "street": ["3 C ST"], "city": ["Z"], "state": ["CA"], "zip": ["90003"]})])
    second = geo.geocode(more, cache, {"batch_size": 10}, fetch=fake)
    assert calls[-1] == ["3 C ST|90003"] and len(second) == 3


def test_geocode_marks_missing_responses(tmp_path):
    addrs = pd.DataFrame({"address_key": ["1 A ST|90001"], "street": ["1 A ST"], "city": ["X"], "state": ["CA"], "zip": ["90001"]})
    out = geo.geocode(addrs, tmp_path / "g.parquet", {}, fetch=lambda rows, s: "")
    assert out.iloc[0]["geo_status"] == "No_Response"


@pytest.mark.parametrize("raw,canon,mt,ok", [
    ("8700 BEVERLY BLVD|90048", "8700 W BEVERLY BLVD|90048", "Non_Exact", True),
    ("600 N GARFIELD AVE|91108", "600 S GARFIELD AVE|91754", "Non_Exact", False),
    ("2055 SAVIERS RD|93033", "2045 SAVIERS RD|93033", "Non_Exact", False),
    ("1 HOAG DR|92663", "1 HOAG DR|92663", "Exact", True),
    ("400 PARNASSUS AVE|94122", "400 PARNASSUS AVE|94143", "Exact", True),
    ("400 PARNASSUS AVE|94122", "400 PARNASSUS AVE|94143", "Non_Exact", False),
])
def test_safe_geocode_rule(raw, canon, mt, ok):
    assert c5m.safe_geocode(raw, canon, mt) is ok


def test_county_label_check():
    refs = pd.DataFrame([{"npi": 1, "tin": "1", "business_name": "PPO: Los Angeles County"},
                         {"npi": 2, "tin": "2", "business_name": "PPO: San Diego County"},
                         {"npi": 3, "tin": "3", "business_name": "Some Clinic"}])
    prov = pd.DataFrame([{"npi": 1, "best_county": "LOS ANGELES", "location_status": "verified"},
                         {"npi": 2, "best_county": "ORANGE", "location_status": "verified"},
                         {"npi": 3, "best_county": "KERN", "location_status": "verified"}])
    out = c5m.county_label_check(refs, prov).set_index("npi")
    assert out.loc[1, "result"] == "agrees" and out.loc[2, "result"] == "differs" and 3 not in out.index


def test_directory_check_scoring():
    df = pd.DataFrame([
        {"stratum": "verified", "directory_found": "Y", "address_matches": "Y"},
        {"stratum": "verified", "directory_found": "Y", "address_matches": "N"},
        {"stratum": "unverified", "directory_found": "N", "address_matches": "N"},
        {"stratum": "unverified", "directory_found": "", "address_matches": ""},
    ])
    out = c5m.score_directory_check(df)
    assert out == {"checked": 3, "found_in_directory": 2, "precision_unverified": 0.0, "checked_unverified": 1,
                   "precision_verified": 0.5, "checked_verified": 2}
    assert c5m.score_directory_check(df.iloc[[3]])["checked"] == 0
