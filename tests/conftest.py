import json

import pytest

from src.common import load_config

PPO = {"plan_name": "CA L PPO 30/500/4000/30%", "plan_id_type": "ein", "plan_id": "952789166", "plan_market_type": "group", "issuer_name": "Healthnet, LLC", "plan_sponsor_name": "Healthnet, LLC"}
HMO = {"plan_name": "CA L SC HMO 50/1500d/7500", "plan_id_type": "ein", "plan_id": "474726731", "plan_market_type": "group", "issuer_name": "Healthnet, LLC", "plan_sponsor_name": "Healthnet, LLC"}
PPO2 = {"plan_name": "Gold PPO 500/20", "plan_id_type": "ein", "plan_id": "67138CA073", "plan_market_type": "group", "issuer_name": "Healthnet, LLC", "plan_sponsor_name": "Healthnet, LLC"}
OR_PLAN = {"plan_name": "OREGON TURF & TREE - PPO BASE PLAN 2 -NN", "plan_id_type": "ein", "plan_id": "930705560", "plan_market_type": "group", "issuer_name": "Healthnet, LLC", "plan_sponsor_name": "Healthnet, LLC"}
IND = {"plan_name": "Ambetter Silver", "plan_id_type": "hios", "plan_id": "67138CA001", "plan_market_type": "individual", "issuer_name": "Ambetter from Centene"}

MIXED_URL = "https://EX.example.com/docs/2026-01-01_x_healthnet-ca_in-network.json"
PPO_ONLY_URL = "https://ex.example.com/docs/2026-01-01_x_healthnet-ca-ppo_in-network.json"
OR_URL = "https://ex.example.com/docs/2026-01-01_x_healthnet-or_in-network.json"


def v2_index():
    return {
        "reporting_entity_name": "Centene Management Company LLC",
        "reporting_entity_type": "Third Party Administrator",
        "last_updated_on": "2026-01-01",
        "version": "2.0.0",
        "reporting_structure": [
            {"reporting_plans": [PPO, HMO], "in_network_files": [{"description": "in-network file", "location": MIXED_URL + "?sig=abc&se=2026"}],
             "allowed_amount_file": {"description": "allowed-amounts file", "location": "https://ex.example.com/docs/aa.json"}},
            {"reporting_plans": [PPO2], "in_network_files": [{"description": "in-network file", "location": "  " + MIXED_URL.lower().replace("ex.example.com", "EX.example.com:443") + "  "},
                                                             {"description": "ppo file", "location": PPO_ONLY_URL}]},
            {"reporting_plans": [OR_PLAN], "in_network_files": [{"description": "in-network file", "location": OR_URL}]},
        ],
    }


def v1_index():
    return {
        "reporting_entity_name": "Centene Management Company LLC",
        "reporting_entity_type": "Third Party Administrator",
        "version": "1.0.0",
        "reporting_structure": [
            {"reporting_plans": [{"plan_name": "Ambetter Silver", "plan_id_type": "hios", "plan_id": "67138CA001", "plan_market_type": "individual"}],
             "in_network_files": [{"location": "https://other.example.com/x/2026-01-01_x_healthnet-ca_in-network.json"}]},
            {"reporting_plans": [IND], "in_network_files": ["https://other.example.com/y/ambetter-ca_in-network.json"]},
            {"reporting_plans": [{"plan_name": "No File Plan", "plan_id_type": "ein", "plan_id": "123456789"}]},
        ],
    }


@pytest.fixture
def index_dir(tmp_path):
    d = tmp_path / "index"
    d.mkdir()
    (d / "2026-01-01_healthnet_index.json").write_text(json.dumps(v2_index()))
    (d / "2026-01-01_ambetter_ca_sub_index (1).json").write_text(json.dumps(v1_index()))
    return d


@pytest.fixture
def cfg(tmp_path, index_dir):
    return load_config(None, {"index_dir": str(index_dir), "output_dir": str(tmp_path / "out"), "workers": 1})
