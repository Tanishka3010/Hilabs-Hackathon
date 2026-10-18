import json

import pandas as pd
import pytest

from src import c3_quality as c3q
from src import ptd_reader, reference_data as rd
from src.common import load_config

NPI_GOOD = 1234567893
NPI_NV = 1770688129
NPI_BAD = 1234567890
FILE_KEY = "testfile0000001"


def price(rate, cls="professional", typ="negotiated", exp="9999-12-31", **extra):
    return {"negotiated_type": typ, "negotiated_rate": rate, "expiration_date": exp, "service_code": ["11"], "billing_class": cls, **extra}


IN_NETWORK = {
    "reporting_entity_name": "Test", "reporting_entity_type": "TPA", "version": "2.0.0", "last_updated_on": "2026-09-01",
    "provider_references": [
        {"provider_group_id": 1, "provider_groups": [{"npi": [NPI_GOOD], "tin": {"type": "ein", "value": "11-1111111"}}]},
        {"provider_group_id": 2, "provider_groups": [{"npi": [NPI_NV, NPI_BAD], "tin": {"type": "ein", "value": "22-2222222"}}]},
    ],
    "in_network": [
        {"negotiation_arrangement": "ffs", "name": "Office visit", "billing_code_type": "CPT", "billing_code": "99213",
         "negotiated_rates": [{"provider_references": [1], "negotiated_prices": [price(100), price(100), price(150)]},
                              {"provider_references": [2], "negotiated_prices": [price(0)]}]},
        {"negotiation_arrangement": "ffs", "name": "INJ X", "billing_code_type": "RC", "billing_code": "636",
         "negotiated_rates": [{"provider_references": [2], "negotiated_prices": [price(50, cls="institutional", additional_information="J1100")]}]},
        {"negotiation_arrangement": "ffs", "name": "ASSAY", "billing_code_type": "RC", "billing_code": "301",
         "negotiated_rates": [{"provider_references": [1], "negotiated_prices": [price(20, cls="institutional")]}]},
        {"negotiation_arrangement": "ffs", "name": "Drug", "billing_code_type": "HCPCS", "billing_code": "J2350",
         "negotiated_rates": [{"provider_references": [1], "negotiated_prices": [price(5, typ="percentage")]}]},
        {"negotiation_arrangement": "ffs", "name": "Visit 2", "billing_code_type": "CPT", "billing_code": "99214",
         "negotiated_rates": [{"provider_references": [1], "negotiated_prices": [price(90, exp="2020-01-01")]}]},
    ],
}


def nppes_row(npi, state, taxonomy):
    row = {k: "" for k in rd.NPPES_RENAME}
    row.update({"NPI": str(npi), "Entity Type Code": "1", "Provider Business Practice Location Address State Name": state,
                "Provider Business Practice Location Address Postal Code": "94107", "Last Update Date": "01/15/2024",
                "Healthcare Provider Taxonomy Code_1": taxonomy})
    return row


def write_benchmark(path):
    with pd.ExcelWriter(path) as xw:
        pd.DataFrame({"SPEC_TYPE": ["Individual", "Individual"], "SPEC_CD": ["A", "B"], "SPEC_DESCR": ["Spec A", "Spec B"],
                      "Taxonomy Code": ["TAXA00000X", "TAXB00000X"]}).to_excel(xw, sheet_name="Taxonomy-Spec Xwalk", index=False)
        pd.DataFrame({"Spec Code": ["A", "B"], "Specialty": ["Spec A", "Spec B"], "Specialty Weight": [100, 50]}).to_excel(
            xw, sheet_name="Specialty Weights", index=False)
        pd.DataFrame({"SPEC_CD": ["A", "A", "B"], "SPEC_DESCRIPTION": ["Spec A", "Spec A", "Spec B"], "PROC_CD": ["99213", "J2350", "27447"],
                      "SPEC_PROC_RANK": [1, 2, 1], "SPEC_PROC_WEIGHT": [60, 40, 10]}).to_excel(xw, sheet_name="Spec-Proc Weights", index=False)


@pytest.fixture
def cfg(tmp_path):
    ref, nppes, out = tmp_path / "ref", tmp_path / "nppes", tmp_path / "out"
    for d in (ref, nppes, out):
        d.mkdir()
    write_benchmark(ref / "Rate Benchmarking Individual Weights_Hackathon.xlsx")
    pd.DataFrame([nppes_row(NPI_GOOD, "CA", "TAXA00000X"), nppes_row(NPI_NV, "NV", "TAXA00000X")]).to_csv(
        nppes / "npidata_pfile_20050523-20260907.csv", index=False)
    pd.DataFrame([{"NPI": str(NPI_GOOD), "State": "CA", "ZIP Code": "94107"}]).reindex(columns=list(rd.DAC_RENAME)).to_csv(
        ref / "DAC_NationalDownloadableFile.csv", index=False)
    src = tmp_path / "2026-09-01_test_in-network.json"
    src.write_text(json.dumps(IN_NETWORK))
    ptd_reader.extract_local(FILE_KEY, "https://example.com/2026-09-01_test_in-network.json", src, out / "c4")
    c = load_config(None, {"reference_dir": str(ref), "nppes_dir": str(nppes), "output_dir": str(out)})
    c["c3"] = {**c["c3"], "outlier_min_group": 2, "as_of_date": None}
    return c


def values(cfg):
    c3q.run(cfg)
    m = pd.read_csv(cfg["output_dir"] / "c3" / "metric_values.csv")
    return {r.metric: (r.raw_value, r.score) for r in m.itertuples()}


def test_reader_counts(cfg):
    h = json.loads((cfg["output_dir"] / "c4" / FILE_KEY / "file_header.json").read_text())
    assert (h["n_in_network_items"], h["n_negotiated_rates"], h["n_negotiated_prices"], h["n_rows"]) == (5, 6, 8, 8)
    assert h["n_provider_references"] == 2 and h["n_provider_rows"] == 3 and h["complete"]


def test_rate_level_metrics(cfg):
    v = values(cfg)
    assert v["zero_invalid_rates"][0] == pytest.approx(1 / 8)
    assert v["zero_invalid_rates"][1] == 0.0
    assert v["exact_duplicates"][0] == pytest.approx(1 / 8)
    assert v["rate_type_usability"][0] == pytest.approx(7 / 8)
    assert v["expired_rates"][0] == pytest.approx(1 / 8)
    assert v["code_identifiability"][0] == pytest.approx(6 / 8)  # RC 636 + J1100: J1100 is not the item's own service, so it is only an associated code
    assert v["rate_replication"][0] == pytest.approx(0.0)
    assert v["referential_integrity"][0] == pytest.approx(1.0)
    assert v["outlier_rate"][0] == pytest.approx(0.0)
    assert v["conflicting_rates"][0] == pytest.approx(1 / 8)
    assert v["file_freshness"][0] == 0


def test_coverage_and_provider_metrics(cfg):
    v = values(cfg)
    assert v["code_coverage"][0] == pytest.approx(60 / 150)
    assert v["provider_validity"][0] == pytest.approx(1 / 3)
    assert v["specialty_code_coverage"][0] == pytest.approx(60 / 150)
    d = json.loads((cfg["output_dir"] / "c3" / "metric_details.json").read_text())[FILE_KEY]
    assert d["provider_validity"]["check_digit_valid"] == 2
    assert d["provider_validity"]["not_in_nppes"] == 1
    assert d["code_coverage"]["codes_priced_any_class"] == 1


def test_missing_nppes_leaves_provider_metrics_unscored(cfg):
    for p in cfg["nppes_dir"].glob("*.csv"):
        p.unlink()
    v = values(cfg)
    assert pd.isna(v["provider_validity"][1]) and pd.isna(v["specialty_code_coverage"][1])
    scores = pd.read_csv(cfg["output_dir"] / "c3" / "file_scores.csv")
    assert scores.loc[0, "metrics_scored"] == len(c3q.METRICS) - 2


@pytest.mark.parametrize("bad,tol,expected", [(0.0, 0.1, 1.0), (0.05, 0.1, 0.5), (0.2, 0.1, 0.0)])
def test_tolerance_score(bad, tol, expected):
    assert c3q.tolerance_score(bad, tol) == pytest.approx(expected)


@pytest.mark.parametrize("days,expected", [(0, 1.0), (31, 1.0), (198, 0.5), (400, 0.0), (None, None)])
def test_freshness_score(days, expected):
    assert c3q.freshness_score(days, 31, 365) == (pytest.approx(expected) if expected is not None else None)


def test_weighted_total_skips_unscored_and_renormalises():
    assert c3q.weighted_total({"a": 1.0, "b": 0.0, "c": None}, {"a": 3, "b": 1, "c": 5}) == pytest.approx(0.75)
    assert c3q.grade(0.91, {"A": 0.9, "B": 0.8, "C": 0.7, "D": 0.6}) == "A"
    assert c3q.grade(0.5, {"A": 0.9, "B": 0.8, "C": 0.7, "D": 0.6}) == "F"
