"""C7: is a provider cost effective compared with similar providers?

Unit: a provider = one NPI billing under one TIN (contracts are signed per TIN, and one NPI can bill under several).
Similar providers = same service, same kind of provider, same market:
  - service: billing code type + code + associated code + service + billing class + setting + place of service + modifier
    (the C4 context, so only like-for-like prices are compared);
  - kind: professional rates -> HiLabs benchmark specialty (falls back to the NUCC classification of the primary taxonomy);
    institutional rates -> NUCC classification (e.g. General Acute Care Hospital vs Ambulatory Surgical);
  - market: the county of the provider's best address; widened to the whole state when the county has fewer than min_peers providers.
Metrics (see docs/logic_c7.md for the reasons): price ratio to the peer median (leave-one-out), cost index (geometric mean of ratios),
percentile, share of services above the peers' 75th percentile, services compared."""
import numpy as np
import pandas as pd

from src import reference_data as rd
from src.common import duck, log, sql_str, write_json

SERVICE_COLS = ["billing_code_type", "billing_code", "associated_code", "service_key", "billing_class", "setting", "place_of_service",
                "billing_code_modifier"]


def provider_services(cfg):
    p = cfg["output_dir"] / "c6" / "provider_rate_detail.parquet"
    con = duck(cfg)
    cols = ", ".join(f"coalesce(CAST({c} AS VARCHAR), '') AS {c}" for c in SERVICE_COLS)
    df = con.execute(f"""
        WITH r AS (SELECT * FROM read_parquet({sql_str(p)}) WHERE npi_valid AND coalesce(negotiation_arrangement, '') <> 'capitation'),
             loc AS (SELECT npi, tin, arg_min(county, coalesce(pair_rank, 999)) AS county, arg_min(address_status, coalesce(pair_rank, 999)) AS address_status
                     FROM r GROUP BY ALL)
        SELECT r.npi, coalesce(r.tin, '') AS tin, any_value(r.provider_name) AS provider_name, any_value(r.entity_type) AS entity_type,
               any_value(r.taxonomy_code) AS taxonomy_code, any_value(r.specialty) AS specialty, any_value(r.hilabs_spec_cd) AS hilabs_spec_cd,
               any_value(r.hilabs_specialty) AS hilabs_specialty, {cols}, any_value(r.service_label) AS service_label,
               any_value(r.procedure_code) AS procedure_code, min(r.rate) AS rate,
               any_value(loc.county) AS county, any_value(loc.address_status) AS address_status
        FROM r LEFT JOIN loc ON loc.npi = r.npi AND loc.tin IS NOT DISTINCT FROM r.tin
        GROUP BY r.npi, coalesce(r.tin, ''), {", ".join(f"coalesce(CAST(r.{c} AS VARCHAR), '')" for c in SERVICE_COLS)}""").df()
    con.close()
    return df


def provider_kind(df, nucc):
    cls = df["taxonomy_code"].map(nucc).fillna("")
    tax_kind = ("tax:" + cls).where(cls != "", "tax:unknown")
    prof = df["billing_class"].str.lower() == "professional"
    return tax_kind.where(~prof | (df["hilabs_spec_cd"].fillna("") == ""), "spec:" + df["hilabs_spec_cd"].astype(str))


def leave_one_out_median(values):
    """Median of the other providers' rates, for every provider in the group (so a provider is never compared with itself)."""
    a = np.asarray(values, dtype=float)
    n = len(a)
    if n < 2:
        return np.full(n, np.nan)
    order = np.argsort(a, kind="stable")
    s = a[order]
    m = n - 1
    pos = np.empty(n, dtype=int)
    pos[order] = np.arange(n)

    def kth(k, i):  # k-th element of the sorted array with position i removed
        return np.where(k < i, s[np.minimum(k, n - 1)], s[np.minimum(k + 1, n - 1)])

    if m % 2:
        return kth(np.full(n, (m - 1) // 2), pos)
    return (kth(np.full(n, m // 2 - 1), pos) + kth(np.full(n, m // 2), pos)) / 2


def compare(df, min_peers):
    """Adds peer level, peer count, leave-one-out peer median, ratio and percentile for every provider-service row."""
    df = df.copy()
    df["service_id"] = df[SERVICE_COLS].agg("|".join, axis=1)
    out = []
    for level, keys in (("county", ["service_id", "kind", "county"]), ("state", ["service_id", "kind"])):
        sub = df if level == "state" else df[df["county"].fillna("") != ""]
        g = sub.groupby(keys, sort=False)["rate"]
        n = g.transform("size")
        ok = n >= min_peers + 1
        part = sub[ok].copy()
        part["peer_level"], part["n_peers"] = level, n[ok] - 1
        part["peer_median"] = part.groupby(keys, sort=False)["rate"].transform(lambda s: pd.Series(leave_one_out_median(s.to_numpy()), index=s.index))
        part["peer_p75"] = part.groupby(keys, sort=False)["rate"].transform(lambda s: s.quantile(0.75))
        part["percentile"] = part.groupby(keys, sort=False)["rate"].rank(pct=True, method="average")
        out.append(part)
    both = pd.concat(out)
    both["level_rank"] = both["peer_level"].map({"county": 0, "state": 1})
    best = both.sort_values("level_rank").drop_duplicates(["npi", "tin", "service_id"]).drop(columns="level_rank")
    best["price_ratio"] = best["rate"] / best["peer_median"]
    return best[best["price_ratio"].notna() & (best["peer_median"] > 0)]


def score_providers(comp, services, c7):
    lo, hi, min_services = float(c7["cost_effective_max_index"]), float(c7["high_cost_min_index"]), int(c7["min_services"])
    comp = comp.assign(log_ratio=np.log(comp["price_ratio"]), above_p75=comp["rate"] > comp["peer_p75"])
    agg = comp.groupby(["npi", "tin"]).agg(
        n_services_compared=("service_id", "size"), cost_index=("log_ratio", lambda s: float(np.exp(s.mean()))),
        median_percentile=("percentile", "median"), share_above_peer_p75=("above_p75", "mean"),
        share_compared_in_county=("peer_level", lambda s: float((s == "county").mean())), median_peers=("n_peers", "median"))
    meta = services.groupby(["npi", "tin"]).agg(provider_name=("provider_name", "first"), entity_type=("entity_type", "first"),
                                                specialty=("specialty", "first"), hilabs_specialty=("hilabs_specialty", "first"),
                                                county=("county", "first"), address_status=("address_status", "first"),
                                                n_services_priced=("service_id", "nunique"))
    p = meta.join(agg, how="left").reset_index()
    p["n_services_compared"] = p["n_services_compared"].fillna(0).astype(int)
    p["coverage_share"] = (p["n_services_compared"] / p["n_services_priced"]).round(4)
    enough = p["n_services_compared"] >= min_services
    p["label"] = np.select([~enough, p["cost_index"] <= lo, p["cost_index"] >= hi], ["insufficient_data", "cost_effective", "high_cost"], "in_line")
    for c in ("cost_index", "median_percentile", "share_above_peer_p75", "share_compared_in_county", "coverage_share"):
        p[c] = p[c].round(4)
    return p.sort_values(["label", "cost_index", "npi", "tin"]).reset_index(drop=True)


def run(cfg):
    c7 = cfg.get("c7") or {}
    out = cfg["output_dir"] / "c7"
    out.mkdir(parents=True, exist_ok=True)
    if not (cfg["output_dir"] / "c6" / "provider_rate_detail.parquet").exists():
        log.warning("c7: needs outputs/c6/provider_rate_detail.parquet; run c6 first")
        return {}
    services = provider_services(cfg)
    nucc = rd.load_nucc(cfg).set_index("taxonomy_code")["classification"]
    services["kind"] = provider_kind(services, nucc)
    services["service_id"] = services[SERVICE_COLS].agg("|".join, axis=1)
    comp = compare(services, int(c7.get("min_peers", 5)))
    prov = score_providers(comp, services, c7)
    keep = ["npi", "tin", "provider_name", "kind", "county", "service_label", "billing_code_type", "billing_code", "associated_code", "procedure_code",
            "billing_class", "setting", "place_of_service", "billing_code_modifier", "rate", "peer_level", "n_peers", "peer_median", "peer_p75",
            "price_ratio", "percentile"]
    comp[keep].sort_values(["npi", "tin", "billing_code_type", "billing_code", "service_label"]).to_parquet(out / "service_comparisons.parquet", index=False)
    prov.to_csv(out / "provider_cost_effectiveness.csv", index=False)
    stats = {"providers": int(len(prov)), "providers_scored": int((prov["label"] != "insufficient_data").sum()),
             "by_label": prov["label"].value_counts().to_dict(), "service_rows_compared": int(len(comp)),
             "service_rows_total": int(len(services)), "compared_at_county_level": int((comp["peer_level"] == "county").sum()),
             "cost_index_quartiles": prov["cost_index"].quantile([0.25, 0.5, 0.75]).round(4).to_dict(),
             "by_entity_type": prov.groupby("entity_type")["label"].value_counts().unstack(fill_value=0).to_dict("index"),
             "config": c7}
    write_json(stats, out / "c7_stats.json")
    from src import c6_rate_file
    c6_rate_file.write_run_summary(cfg)
    log.info("c7: %d providers, %d scored: %s", stats["providers"], stats["providers_scored"], stats["by_label"])
    return stats
