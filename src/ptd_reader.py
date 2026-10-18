import gzip
import json
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import ijson
import pyarrow as pa
import pyarrow.parquet as pq

from src.common import log, url_file_name, write_json

BATCH_ROWS = 250_000

RATE_SCHEMA = pa.schema([
    ("file_key", pa.string()),
    ("in_network_idx", pa.int64()),
    ("negotiation_arrangement", pa.string()),
    ("billing_code_type", pa.string()),
    ("billing_code_type_version", pa.string()),
    ("billing_code", pa.string()),
    ("name", pa.string()),
    ("description", pa.string()),
    ("rate_idx", pa.int64()),
    ("price_idx", pa.int64()),
    ("provider_group_id", pa.string()),
    ("negotiated_type", pa.string()),
    ("negotiated_rate", pa.float64()),
    ("expiration_date", pa.string()),
    ("billing_class", pa.string()),
    ("setting", pa.string()),
    ("service_code", pa.string()),
    ("billing_code_modifier", pa.string()),
    ("additional_information", pa.string()),
])

REF_SCHEMA = pa.schema([
    ("file_key", pa.string()),
    ("provider_group_id", pa.string()),
    ("npi", pa.int64()),
    ("tin_type", pa.string()),
    ("tin", pa.string()),
    ("business_name", pa.string()),
    ("network_name", pa.string()),
])


def open_url(req, timeout, retries=3, backoff=5):
    for attempt in range(retries):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == retries - 1:
                raise
            log.warning("http: %s failed (%s), retry %d", req.full_url, type(e).__name__, attempt + 1)
            time.sleep(backoff * (attempt + 1))


def download(url, dest, chunk=8 * 1024 * 1024, retries=3):
    """Resumable download. A resume is only trusted when the server answers 206; otherwise the file is fetched from byte 0."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    head = open_url(urllib.request.Request(url, method="HEAD"), 60, retries)
    total = int(head.headers.get("Content-Length") or 0)
    for attempt in range(retries):
        have = dest.stat().st_size if dest.exists() else 0
        if total and have == total:
            log.info("download: %s already complete (%s bytes)", dest.name, f"{total:,}")
            return dest, total
        if total and have > total:
            dest.unlink()
            have = 0
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        log.info("download: %s from byte %s of %s", dest.name, f"{have:,}", f"{total:,}")
        try:
            with open_url(req, 120, retries) as r:
                resumed = have and getattr(r, "status", 200) == 206
                with open(dest, "ab" if resumed else "wb") as fh:
                    while True:
                        block = r.read(chunk)
                        if not block:
                            break
                        fh.write(block)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            log.warning("download: interrupted (%s), attempt %d", type(e).__name__, attempt + 1)
            time.sleep(5 * (attempt + 1))
            continue
        size = dest.stat().st_size
        if not total or size == total:
            return dest, total or size
        log.warning("download: got %s of %s bytes, attempt %d", f"{size:,}", f"{total:,}", attempt + 1)
    raise IOError(f"download incomplete: {dest.name}")


def is_gzip(path):
    with open(path, "rb") as fh:
        return fh.read(2) == b"\x1f\x8b"


def open_json(path):
    """Opens plain or gzip-compressed JSON (detected from the magic bytes, not the file name) as a binary stream."""
    return gzip.open(path, "rb") if is_gzip(path) else open(path, "rb")


def to_npi(v, invalid, reason="non_numeric_npi"):
    try:
        return int(v)
    except (TypeError, ValueError):
        invalid[reason] += 1
        return None


def fetch_remote_groups(location, cache_dir):
    """TiC provider_references may point by URL to a separate file holding provider_groups; fetched once and cached."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    local = cache_dir / (url_file_name(location) or "provider_reference.json")
    if not local.exists():
        with open_url(urllib.request.Request(location), 120) as r:
            local.write_bytes(r.read())
    with open_json(local) as fh:
        doc = json.load(fh)
    return doc.get("provider_groups") or []


def text(v):
    if v is None:
        return None
    if isinstance(v, list):
        return "|".join(str(x) for x in v)
    return str(v)


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class BatchWriter:
    def __init__(self, path, schema):
        self.path, self.schema, self.rows, self.writer, self.count = path, schema, [], None, 0

    def add(self, row):
        self.rows.append(row)
        if len(self.rows) >= BATCH_ROWS:
            self.flush()

    def flush(self):
        if not self.rows:
            return
        table = pa.Table.from_pylist(self.rows, schema=self.schema)
        if self.writer is None:
            self.writer = pq.ParquetWriter(self.path, self.schema, compression="zstd")
        self.writer.write_table(table)
        self.count += len(self.rows)
        self.rows = []

    def close(self):
        self.flush()
        if self.writer is None:
            pq.ParquetWriter(self.path, self.schema, compression="zstd").close()
        else:
            self.writer.close()
        return self.count


def read_header_fields(path):
    out = {}
    with open_json(path) as fh:
        for prefix, event, value in ijson.parse(fh):
            if prefix in ("reporting_entity_name", "reporting_entity_type", "version", "last_updated_on") and event in ("string", "number"):
                out[prefix] = value
            if prefix in ("provider_references", "in_network") and event == "start_array":
                break
    return out


def write_provider_refs(path, file_key, out_path, invalid, remote_cache=None):
    w = BatchWriter(out_path, REF_SCHEMA)
    n_refs, ids = 0, set()
    with open_json(path) as fh:
        for pr in ijson.items(fh, "provider_references.item", use_float=True):
            n_refs += 1
            gid = text(pr.get("provider_group_id"))
            if gid is None:
                invalid["provider_reference_without_id"] += 1
                continue
            ids.add(gid)
            network = text(pr.get("network_name")) or ""
            groups = pr.get("provider_groups") or []
            if not groups and pr.get("location"):
                try:
                    groups = fetch_remote_groups(pr["location"], remote_cache or Path(out_path).parent / "remote_refs")
                except Exception as e:
                    log.warning("provider reference %s at %s unreachable (%s)", gid, pr["location"], type(e).__name__)
                    invalid["remote_provider_reference_unreachable"] += 1
            if not groups:
                invalid["provider_reference_without_groups"] += 1
            for g in groups:
                tin = g.get("tin") or {}
                npis = g.get("npi") or []
                if not npis:
                    invalid["provider_group_without_npi"] += 1
                    w.add({"file_key": file_key, "provider_group_id": gid, "npi": None, "tin_type": text(tin.get("type")),
                           "tin": text(tin.get("value")), "business_name": text(tin.get("business_name")), "network_name": network})
                for npi in npis:
                    npi_int = to_npi(npi, invalid)
                    w.add({"file_key": file_key, "provider_group_id": gid, "npi": npi_int, "tin_type": text(tin.get("type")),
                           "tin": text(tin.get("value")), "business_name": text(tin.get("business_name")), "network_name": network})
    return n_refs, ids, w.close()


def write_rates(path, file_key, out_path, ref_out_path, invalid):
    w = BatchWriter(out_path, RATE_SCHEMA)
    inline = BatchWriter(ref_out_path, REF_SCHEMA)
    n_items = n_rates = n_prices = inline_n = 0
    with open_json(path) as fh:
        for i, item in enumerate(ijson.items(fh, "in_network.item", use_float=True)):
            n_items += 1
            base = {"file_key": file_key, "in_network_idx": i, "negotiation_arrangement": text(item.get("negotiation_arrangement")),
                    "billing_code_type": text(item.get("billing_code_type")), "billing_code_type_version": text(item.get("billing_code_type_version")),
                    "billing_code": text(item.get("billing_code")), "name": text(item.get("name")),
                    "description": text(item.get("description"))}
            if not base["billing_code"]:
                invalid["in_network_without_billing_code"] += 1
            for j, nr in enumerate(item.get("negotiated_rates") or []):
                n_rates += 1
                gids = [text(x) for x in (nr.get("provider_references") or [])]
                for g in nr.get("provider_groups") or []:
                    inline_n += 1
                    gid = f"inline-{i}-{j}-{inline_n}"
                    gids.append(gid)
                    tin = g.get("tin") or {}
                    for npi in g.get("npi") or [None]:
                        inline.add({"file_key": file_key, "provider_group_id": gid, "npi": to_npi(npi, invalid) if npi is not None else None,
                                    "tin_type": text(tin.get("type")), "tin": text(tin.get("value")),
                                    "business_name": text(tin.get("business_name")), "network_name": ""})
                if not gids:
                    invalid["negotiated_rate_without_provider"] += 1
                    gids = [None]
                for k, price in enumerate(nr.get("negotiated_prices") or []):
                    n_prices += 1
                    rate = to_float(price.get("negotiated_rate"))
                    if rate is None:
                        invalid["non_numeric_negotiated_rate"] += 1
                    row = {**base, "rate_idx": j, "price_idx": k, "negotiated_type": text(price.get("negotiated_type")), "negotiated_rate": rate,
                           "expiration_date": text(price.get("expiration_date")), "billing_class": text(price.get("billing_class")),
                           "setting": text(price.get("setting")), "service_code": text(price.get("service_code")),
                           "billing_code_modifier": text(price.get("billing_code_modifier")),
                           "additional_information": text(price.get("additional_information"))}
                    for gid in gids:
                        w.add({**row, "provider_group_id": gid})
            if n_items % 2000 == 0:
                log.info("  ... %s in_network items, %s rate rows", f"{n_items:,}", f"{w.count + len(w.rows):,}")
    return n_items, n_rates, n_prices, w.close(), inline.close()


def extract_file(file_key, url, ptd_dir, out_root, parallel=True):
    local, total = download(url, Path(ptd_dir) / url_file_name(url))
    return extract_local(file_key, url, local, out_root, total, parallel)


def _refs_pass(local, file_key, out_dir):
    invalid = Counter()
    n_refs, _, ref_rows = write_provider_refs(local, file_key, out_dir / "provider_refs.parquet", invalid)
    return n_refs, ref_rows, invalid


def _rates_pass(local, file_key, out_dir):
    invalid = Counter()
    res = write_rates(local, file_key, out_dir / "rates.parquet", out_dir / "provider_refs_inline.parquet", invalid)
    return res, invalid


def extract_local(file_key, url, local, out_root, total=None, parallel=True):
    local = Path(local)
    total = total or local.stat().st_size
    out_dir = Path(out_root) / file_key
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("extract: %s (%s bytes)", local.name, f"{total:,}")
    hdr = read_header_fields(local)
    if parallel:
        with ProcessPoolExecutor(max_workers=2) as ex:
            fr, fp = ex.submit(_refs_pass, local, file_key, out_dir), ex.submit(_rates_pass, local, file_key, out_dir)
            (n_refs, ref_rows, inv_refs), (rates_res, inv_rates) = fr.result(), fp.result()
    else:
        n_refs, ref_rows, inv_refs = _refs_pass(local, file_key, out_dir)
        rates_res, inv_rates = _rates_pass(local, file_key, out_dir)
    invalid = inv_refs + inv_rates
    n_items, n_rates, n_prices, rows, inline_rows = rates_res
    log.info("extract: %s provider_references -> %s provider rows", f"{n_refs:,}", f"{ref_rows:,}")
    header = {
        "file_key": file_key, "file_url": url, "reporting_entity_name": hdr.get("reporting_entity_name"),
        "reporting_entity_type": hdr.get("reporting_entity_type"), "version": hdr.get("version"), "last_updated_on": hdr.get("last_updated_on"),
        "bytes_read": local.stat().st_size, "bytes_total": total, "complete": local.stat().st_size == total,
        "compressed": is_gzip(local),
        "n_provider_references": n_refs, "n_provider_rows": ref_rows, "n_inline_provider_rows": inline_rows, "n_in_network_items": n_items,
        "n_negotiated_rates": n_rates, "n_negotiated_prices": n_prices, "n_rows": rows, "invalid_records": dict(sorted(invalid.items())),
    }
    write_json(header, out_dir / "file_header.json")
    log.info("extract: %s items, %s negotiated_rates, %s prices -> %s rate rows; invalid %s", f"{n_items:,}", f"{n_rates:,}",
             f"{n_prices:,}", f"{rows:,}", dict(invalid))
    return header


def load_header(file_dir):
    return json.loads((Path(file_dir) / "file_header.json").read_text())
