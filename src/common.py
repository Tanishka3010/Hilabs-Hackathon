import hashlib
import json
import logging
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import ijson
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
PATH_KEYS = ("index_dir", "nppes_dir", "ptd_dir", "reference_dir", "output_dir")
INDEX_NAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<brand>[a-z]+)(?:_(?P<sub>[a-z0-9_]+?))?_index(?:\s*\(\d+\))?\.json$",
    re.I,
)
DEFAULT_PORTS = {"http": "80", "https": "443"}
SAFE_PATH_CHARS = "/-._~!$&'()*+,;=:@"

log = logging.getLogger("ptd")


def setup_logging(level=logging.INFO):
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s | %(message)s", datefmt="%H:%M:%S")


def resolve_path(value):
    p = Path(value).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()


def load_config(path=None, overrides=None):
    cfg_path = resolve_path(path) if path else ROOT / "config" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg = cfg or {}
    for extra in sorted((cfg_path.parent / "stages").glob("*.yaml")):
        for k, v in (yaml.safe_load(extra.read_text()) or {}).items():
            cfg.setdefault(k, v)
    for k, v in (overrides or {}).items():
        if v is not None:
            cfg[k] = v
    for k in PATH_KEYS:
        if cfg.get(k):
            cfg[k] = resolve_path(cfg[k])
    return cfg


def parse_index_filename(name):
    m = INDEX_NAME_RE.match(name)
    if not m:
        return None, Path(name).stem.split("_")[0].lower(), ""
    return m.group("date"), m.group("brand").lower(), (m.group("sub") or "").lower()


def list_index_files(index_dir):
    return sorted(p for p in Path(index_dir).glob("*.json") if p.is_file())


def normalize_url(url):
    u = (url or "").strip()
    if not u:
        return ""
    s = urlsplit(u)
    scheme = s.scheme.lower()
    host = (s.hostname or "").lower()
    port = s.port
    netloc = host if port is None or str(port) == DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
    path = quote(unquote(s.path), safe=SAFE_PATH_CHARS)
    return urlunsplit((scheme, netloc, path, "", ""))


def url_file_name(url):
    return PurePosixPath(unquote(urlsplit(url).path)).name if url else ""


def stable_key(text, n=16):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def iter_reporting_structure(path):
    with open(path, "rb") as fh:
        yield from ijson.items(fh, "reporting_structure.item", use_float=True)


def read_header(path):
    out = {}
    with open(path, "rb") as fh:
        for prefix, event, value in ijson.parse(fh):
            if prefix in ("reporting_entity_name", "reporting_entity_type", "version", "last_updated_on") and event in ("string", "number"):
                out[prefix] = value
            if prefix == "reporting_structure" and event == "start_array":
                break
    return out


def as_file_entries(raw):
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for it in items:
        if isinstance(it, str):
            out.append({"description": "", "location": it})
        elif isinstance(it, dict):
            out.append({"description": it.get("description") or "", "location": it.get("location") or it.get("url") or ""})
    return out


def write_table(df, out_dir, name):
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_dir / f"{name}.parquet", index=False)
    df.to_csv(out_dir / f"{name}.csv", index=False)


def write_json(obj, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")


def join_sorted(values, sep="|"):
    return sep.join(sorted({str(v) for v in values if v is not None and str(v) != ""}))


def to_frame(rows, columns):
    return pd.DataFrame(rows, columns=columns)
