import pytest

from src.common import normalize_url, parse_index_filename, stable_key, url_file_name


@pytest.mark.parametrize("raw,expected", [
    ("https://WWW.Centene.com/a/B.json", "https://www.centene.com/a/B.json"),
    ("  https://www.centene.com/a/b.json  ", "https://www.centene.com/a/b.json"),
    ("https://www.centene.com/a/b.json?sig=xyz&se=2026&utm_source=x", "https://www.centene.com/a/b.json"),
    ("https://www.centene.com/a/b.json#frag", "https://www.centene.com/a/b.json"),
    ("https://www.centene.com:443/a/b.json", "https://www.centene.com/a/b.json"),
    ("http://www.centene.com:8080/a/b.json", "http://www.centene.com:8080/a/b.json"),
    ("https://www.centene.com/Centene%20Corporate/b.json", "https://www.centene.com/Centene%20Corporate/b.json"),
    ("https://www.centene.com/Centene Corporate/b.json", "https://www.centene.com/Centene%20Corporate/b.json"),
    ("https://www.centene.com/Centene%20corporate/b.json", "https://www.centene.com/Centene%20corporate/b.json"),
    ("", ""),
    (None, ""),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_normalize_keeps_path_case_distinct():
    assert normalize_url("https://x.com/A.json") != normalize_url("https://x.com/a.json")


def test_url_file_name_unquotes():
    assert url_file_name("https://x.com/Centene%20Corporate/f%20g.json") == "f g.json"


@pytest.mark.parametrize("name,expected", [
    ("2026-09-30_healthnet_index.json", ("2026-09-30", "healthnet", "")),
    ("2026-09-28_ambetter_index (1).json", ("2026-09-28", "ambetter", "")),
    ("2026-09-28_qualchoice_city_of_fort_smith_index.json", ("2026-09-28", "qualchoice", "city_of_fort_smith")),
    ("2026-09-28_fidelis_index (2).json", ("2026-09-28", "fidelis", "")),
])
def test_parse_index_filename(name, expected):
    assert parse_index_filename(name) == expected


def test_stable_key_is_deterministic():
    assert stable_key("abc") == stable_key("abc") and len(stable_key("abc")) == 16 and stable_key("abc") != stable_key("abd")
