import csv
import io
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from src.common import log

CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
RESULT_COLS = ["address_key", "geo_status", "geo_match_type", "geo_address", "longitude", "latitude", "state_fips", "county_fips", "tract"]


def multipart(fields, file_field, file_name, file_bytes):
    boundary = uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{file_name}\"\r\nContent-Type: text/csv\r\n\r\n".encode())
    out.write(file_bytes)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def census_fetch(rows, settings):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    body, ctype = multipart({"benchmark": settings.get("benchmark", "Public_AR_Current"), "vintage": settings.get("vintage", "Current_Current")},
                            "addressFile", "addresses.csv", buf.getvalue().encode())
    req = urllib.request.Request(settings.get("url", CENSUS_BATCH_URL), data=body, headers={"Content-Type": ctype}, method="POST")
    for attempt in range(int(settings.get("retries", 3))):
        try:
            with urllib.request.urlopen(req, timeout=int(settings.get("timeout_s", 600))) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            log.warning("geocode: batch of %d failed (%s), attempt %d", len(rows), type(e).__name__, attempt + 1)
            time.sleep(5 * (attempt + 1))
    raise IOError("census geocoder unavailable")


def parse_census(text):
    rows = []
    for rec in csv.reader(io.StringIO(text)):
        if not rec:
            continue
        rec += [""] * (12 - len(rec))
        lon, lat = (rec[5].split(",") + [""])[:2] if rec[5] else ("", "")
        rows.append({"address_key": rec[0], "geo_status": rec[2], "geo_match_type": rec[3], "geo_address": rec[4],
                     "longitude": lon, "latitude": lat, "state_fips": rec[8], "county_fips": rec[9], "tract": rec[10]})
    return pd.DataFrame(rows, columns=RESULT_COLS)


def geocode(addresses, cache_path, settings, fetch=census_fetch):
    cache_path = Path(cache_path)
    cached = pd.read_parquet(cache_path) if cache_path.exists() else pd.DataFrame(columns=RESULT_COLS)
    todo = addresses[~addresses["address_key"].isin(set(cached["address_key"]))].drop_duplicates("address_key")
    if len(todo):
        size = int(settings.get("batch_size", 2500))
        batches = [todo.iloc[i:i + size] for i in range(0, len(todo), size)]
        log.info("geocode: %d new addresses in %d batch(es), %d cached", len(todo), len(batches), len(cached))

        def run(b):
            rows = [[k, s, c, st, z] for k, s, c, st, z in zip(b["address_key"], b["street"], b["city"], b["state"], b["zip"])]
            return parse_census(fetch(rows, settings))

        with ThreadPoolExecutor(max_workers=int(settings.get("workers", 4))) as ex:
            results = list(ex.map(run, batches))
        fresh = pd.concat(results, ignore_index=True) if results else pd.DataFrame(columns=RESULT_COLS)
        missing = set(todo["address_key"]) - set(fresh["address_key"])
        if missing:
            fresh = pd.concat([fresh, pd.DataFrame({"address_key": sorted(missing), "geo_status": "No_Response"})], ignore_index=True)
        cached = pd.concat([cached, fresh], ignore_index=True).drop_duplicates("address_key", keep="last").sort_values("address_key")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cached.reindex(columns=RESULT_COLS).to_parquet(cache_path, index=False)
    out = addresses[["address_key"]].drop_duplicates().merge(cached, on="address_key", how="left")
    out["geo_status"] = out["geo_status"].fillna("Not_Geocoded")
    return out
