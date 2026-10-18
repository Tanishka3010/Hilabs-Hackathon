import numpy as np
import pandas as pd
import pytest

from src import c7_cost_effectiveness as c7

CFG = {"min_peers": 2, "min_services": 2, "cost_effective_max_index": 0.9, "high_cost_min_index": 1.1}


@pytest.mark.parametrize("values,expected", [
    ([10, 20, 30], [25, 20, 15]),          # others: {20,30} {10,30} {10,20}
    ([10, 20, 30, 40], [30, 30, 20, 20]),  # others: {20,30,40} {10,30,40} {10,20,40} {10,20,30}
    ([5, 5, 5], [5, 5, 5]),
])
def test_leave_one_out_median(values, expected):
    assert c7.leave_one_out_median(values).tolist() == expected


def services():
    rows = []
    for npi, mult in ((1, 0.5), (2, 1.0), (3, 1.0), (4, 2.0)):
        for code, base in (("A", 100.0), ("B", 50.0)):
            rows.append({"npi": npi, "tin": str(npi), "provider_name": f"P{npi}", "entity_type": "1", "taxonomy_code": "T", "specialty": "S",
                         "hilabs_spec_cd": "1", "hilabs_specialty": "Spec", "billing_code_type": "CPT", "billing_code": code, "associated_code": "",
                         "service_key": code, "billing_class": "professional", "setting": "outpatient", "place_of_service": "11",
                         "billing_code_modifier": "", "service_label": code, "procedure_code": code, "rate": base * mult,
                         "county": "ALAMEDA", "address_status": "verified"})
    df = pd.DataFrame(rows)
    df["kind"] = c7.provider_kind(df, pd.Series({"T": "Clinic"}))
    df["service_id"] = df[c7.SERVICE_COLS].agg("|".join, axis=1)
    return df


def test_peer_comparison_and_labels():
    s = services()
    comp = c7.compare(s, CFG["min_peers"])
    assert set(comp["peer_level"]) == {"county"} and (comp["n_peers"] == 3).all() and set(comp["kind"]) == {"spec:1"}
    prov = c7.score_providers(comp, s, CFG).set_index("npi")
    assert prov.loc[1, "label"] == "cost_effective" and prov.loc[4, "label"] == "high_cost" and prov.loc[2, "label"] == "in_line"
    assert prov.loc[1, "cost_index"] == pytest.approx(0.5) and prov.loc[1, "n_services_compared"] == 2


def test_small_county_widens_to_state_and_too_few_is_unscored():
    s = services()
    s.loc[s["npi"] == 4, "county"] = "INYO"
    comp = c7.compare(s, CFG["min_peers"])
    assert set(comp.loc[comp["npi"] == 4, "peer_level"]) == {"state"}
    prov = c7.score_providers(c7.compare(s[s["npi"] <= 2], CFG["min_peers"]), s[s["npi"] <= 2], CFG)
    assert set(prov["label"]) == {"insufficient_data"} and np.isnan(prov["cost_index"]).all()
