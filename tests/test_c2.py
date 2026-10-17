import json

import pandas as pd
import pytest

from src import c1_parse_index, c2_classify_network as c2
from tests.conftest import MIXED_URL, PPO_ONLY_URL

GLUED = c2.glued_re(["CUS"])


@pytest.mark.parametrize("name,expected", [
    ("CA L PPO 30/500/4000/30%", "PPO"),
    ("ca l ppo 30/500", "PPO"),
    ("CA L SC HMO 50/1500d/7500", "HMO"),
    ("Salud HMO y Mas Gold $30", "HMO"),
    ("CA L CUS POS County of Kern GF", "POS"),
    ("CA L EOA 20/0/1500", "EOA"),
    ("Health Net HSP Silver", "HSP"),
    ("VIP HOSPITALITY GROUP - PPO5000", "PPO"),
    ("Gold PPO-HSA 1500", "PPO"),
    ("HMO/PPO Dual Option", "MULTI:HMO+PPO"),
    ("CA L CUS PPO CA IRONWORKERS FWP-EPO$10", "PPO+EPO"),
    ("CA L CUS PPO UC POST DOC", "PPO"),
    ("CA L CUS HMO UC POST DOC", "HMO"),
    ("SUPPORTIVE SERVICES POSTAL", ""),
    ("CA L CUSPPO 15/250/20% RAP", ""),
    ("SHMOOZE CORP", ""),
    ("CA L SAL Mex 5/0/1500", ""),
])
def test_token_label(name, expected):
    assert c2.token_label(c2.find_tokens(name)) == expected


@pytest.mark.parametrize("name,expected", [
    ("CA L CUSPPO 15/250/20% RAP", "PPO"),
    ("CA L CUSHMO 20/500", "HMO"),
    ("CA L CUS PPO 15/250", ""),
    ("FOCUSPPO", ""),
])
def test_glued_token(name, expected):
    assert c2.token_label(c2.find_glued(name, GLUED)) == expected


@pytest.mark.parametrize("fname,state", [
    ("2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json", "CA"),
    ("2026-09-30_centene-management-company-llc_healthnet-or_in-network.json", "OR"),
    ("2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json", ""),
    ("2026-09-28_centene-management-company-llc_qualchoice_in-network.json", ""),
    ("2026-01-01_x_healthnet-ca-ppo_in-network.json", "CA"),
])
def test_url_state(fname, state):
    assert c2.url_state(fname) == state


@pytest.mark.parametrize("label,cfg,expected", [
    ("EOA", {"eoa_as": "unknown"}, (c2.UNKNOWN, "low", "EOA_UNRESOLVED")),
    ("EOA", {"eoa_as": "eoa", "organizer_confirmed": ["eoa_as"]}, ("EOA", "high", "P1x_EOA_AS_EOA_ORGANIZER_CONFIRMED")),
    ("EOA", {"eoa_as": "ppo"}, ("PPO", "low", "P1x_EOA_AS_PPO_BY_CONFIG")),
    ("PPO+EPO", {"ppo_epo_as": "conflict"}, (c2.UNKNOWN, "low", "PPO_EPO_CONFLICT")),
    ("PPO+EPO", {"ppo_epo_as": "ppo", "organizer_confirmed": ["ppo_epo_as"]}, ("PPO", "high", "P1x_PPO_EPO_AS_PPO_ORGANIZER_CONFIRMED")),
    ("PPO+EPO", {"ppo_epo_as": "epo"}, ("EPO", "low", "P1x_PPO_EPO_AS_EPO_BY_CONFIG")),
    ("MULTI:HMO+PPO", {}, (c2.UNKNOWN, "low", "MULTI_NETWORK_TOKEN_CONFLICT")),
])
def test_special_mapping(label, cfg, expected):
    assert c2.special_mapping(label, cfg) == expected


def test_family_purity_counts_eoa_as_its_own_network():
    cfg2 = {"eoa_as": "eoa"}
    df = pd.DataFrame({"plan_name": ["CA L EC HMO 1", "CA L EC HMO 2", "CA L EC EOA 3", "CA L SC HMO 4", "CA L SC HMO 5"], "scope_verdict": ["IN_SCOPE"] * 5})
    df["name_label"] = df["plan_name"].map(lambda n: c2.token_label(c2.find_tokens(n)))
    df["explicit_label"] = df["name_label"].map(lambda l: c2.explicit_label(l, cfg2))
    fams = {f["family"]: f for f in c2.validate_families(df, ["EC", "SC"], 2, 1.0)}
    assert not fams["EC"]["validated"] and fams["EC"]["types"] == {"HMO": 2, "EOA": 1}
    assert fams["SC"]["validated"] and fams["SC"]["implied_type"] == "HMO"


def base_row(**kw):
    r = {"name_label": "", "glued_label": "", "file_signal": "", "entity_signal": "", "family_signal": "", "propagation_signal": "", "costshare_signal": ""}
    r.update(kw)
    return pd.Series(r)


def test_precedence_order():
    assert c2.decide(base_row(name_label="HMO", file_signal="PPO", family_signal="PPO"), {})[2] == "P1_PLAN_NAME_TOKEN"
    assert c2.decide(base_row(file_signal="PPO", family_signal="HMO"), {})[:2] == ("PPO", "high")
    assert c2.decide(base_row(entity_signal="HMO", family_signal="PPO"), {})[2] == "P3_LEGAL_ENTITY"
    assert c2.decide(base_row(family_signal="HMO", propagation_signal="PPO"), {})[2] == "P4_PRODUCT_FAMILY"
    assert c2.decide(base_row(propagation_signal="PPO", costshare_signal="HMO"), {})[2] == "P5_FILE_PROPAGATION"
    assert c2.decide(base_row(costshare_signal="PPO"), {})[:2] == ("PPO", "low")
    assert c2.decide(base_row(), {})[2] == "NO_NETWORK_EVIDENCE"
    assert c2.decide(base_row(name_label="EOA", family_signal="HMO"), {"eoa_as": "unknown"})[2] == "EOA_UNRESOLVED"


def run_all(cfg):
    c1_parse_index.run(cfg)
    stats = c2.run(cfg)
    d = cfg["output_dir"] / "c2"
    return stats, pd.read_csv(d / "plan_classification.csv", keep_default_na=False), pd.read_csv(d / "file_classification.csv", keep_default_na=False), d


def test_c2_fixture_end_to_end(cfg):
    stats, plans, files, d = run_all(cfg)
    by = plans.set_index("plan_name")
    assert by.loc["CA L PPO 30/500/4000/30%", "is_target_ppo"]
    assert by.loc["Gold PPO 500/20", "is_target_ppo"]
    assert by.loc["CA L SC HMO 50/1500d/7500", "network_label"] == "HMO"
    assert by.loc["OREGON TURF & TREE - PPO BASE PLAN 2 -NN", "scope_reason"] == "OUT_OF_STATE"
    assert by.loc["Ambetter Silver", "scope_reason"] == "INDIVIDUAL_MARKET"
    assert by.loc["No File Plan", "scope_reason"] == "MARKET_UNKNOWN"
    assert "ID_SHAPE_CONFLICT_EIN_TYPED_HIOS_VALUE" in by.loc["Gold PPO 500/20", "data_flags"]
    f = files.set_index("file_url")
    assert f.loc[MIXED_URL.replace("EX.", "ex."), "network_label"] == "MIXED"
    assert f.loc[PPO_ONLY_URL, "network_label"] == "PPO"
    assert f.loc[PPO_ONLY_URL, "file_token_label"] == "PPO"
    ppo_files = pd.read_csv(d / "ppo_files.csv")
    assert set(ppo_files["file_url"]) == {MIXED_URL.replace("EX.", "ex."), PPO_ONLY_URL}
    assert dict(zip(ppo_files["file_url"], ppo_files["requires_in_file_network_filter"])) == {MIXED_URL.replace("EX.", "ex."): True, PPO_ONLY_URL: False}
    conf = pd.read_csv(d / "conflicts.csv")
    assert (conf["level"] == "file").sum() == 1
    assert stats["ppo_plans"] == 2


def test_c2_config_switch_changes_counts(cfg):
    base, *_ = run_all(cfg)
    cfg["c2"] = {**(cfg.get("c2") or {}), "eoa_as": "ppo"}
    alt, *_ = run_all(cfg)
    assert base["ppo_plans"] == alt["ppo_plans"]
    assert json.loads(json.dumps(alt["config"]))["eoa_as"] == "ppo"


def test_c2_idempotent(cfg):
    run_all(cfg)
    d = cfg["output_dir"] / "c2"
    a = {p.name: p.read_bytes() for p in d.glob("*.csv")}
    run_all(cfg)
    assert a == {p.name: p.read_bytes() for p in d.glob("*.csv")}
