# C7: is a provider cost effective compared with similar providers?

Reproduce (needs C6):

```bash
.venv/bin/python -m src.run --stage c7
```

Outputs in `outputs/c7/`:
- `provider_cost_effectiveness.csv`: one row per provider (NPI × TIN) with its metrics and label;
- `service_comparisons.parquet`: every provider × service compared, with its peer group, peer median and ratio;
- `c7_stats.json`.

## 1. What is compared

**The provider** is one NPI billing under one TIN. Contracts are signed per TIN, and one NPI can bill under several TINs at different prices, so they are scored separately.

**A service** is the C4 rate context: billing code type, code, associated code, service, billing class, setting, place of service and modifier. Only identical services are compared. A facility rate for revenue code 450 is never compared with a professional rate for CPT 99284. Capitation rows are left out, because they are not per-service prices.

## 2. What "similar" means

Two providers are similar for a service when all three of these hold.

| Dimension | Rule | Why |
|---|---|---|
| Same service | The context above | Price differences must not come from comparing different services or settings |
| Same kind of provider | Professional rates: the same HiLabs benchmark specialty (crosswalk from the NPI's primary NPPES taxonomy), falling back to the NUCC classification. Institutional rates: the same NUCC classification, such as General Acute Care Hospital or Ambulatory Surgical | A cardiologist's office visit and a nurse practitioner's are priced differently for legitimate reasons. So are an acute hospital's and a surgery centre's facility fees |
| Same market | The county of the provider's best C5 address. If the county has fewer than `min_peers` (5) other providers for that service and kind, the group widens to the whole state, and `peer_level` records which was used | Prices vary by local market. Too small a group makes the median noisy |

## 3. Metrics

| Metric | Definition | Why |
|---|---|---|
| `price_ratio` (per service) | Provider's rate ÷ median rate of its peers. The median leaves the provider out, so nobody is compared with themselves | The median is robust to the outliers C3 found. A ratio is unit-free, so services of very different prices can be combined |
| `cost_index` | Geometric mean of the provider's price ratios | It is a typical service, not the expensive ones: a 2× and a 0.5× cancel out, whereas an arithmetic mean would be pulled up by large ratios |
| `median_percentile` | Median of the provider's within-peer percentile ranks | Shows position, not just distance. It is useful when peer prices are tightly bunched |
| `share_above_peer_p75` | Share of the provider's services priced above its peers' 75th percentile | Separates "high on everything" from "high on a few services" |
| `n_services_compared`, `coverage_share` | Services with a peer group, and that number ÷ services priced | How much evidence stands behind the index |
| `share_compared_in_county` | Share of comparisons made at county level rather than state level | How local the comparison is |

**Label.** A provider gets a label only after at least `min_services` (5) services were compared; otherwise it is `insufficient_data`.

| Label | Condition |
|---|---|
| `cost_effective` | `cost_index` ≤ 0.90: at least 10% below similar providers on a typical service |
| `high_cost` | `cost_index` ≥ 1.10 |
| `in_line` | In between |

All thresholds are in `config/stages/c7.yaml`.

## 4. Limitations
- **Price, not total cost.** Cost effectiveness here means price per service. Utilisation, quality and outcomes are not in the price transparency data.
- **Labels mostly reach organisations.** Many individual clinicians price only a few services, or their specialty has few peers in their county. They stay `insufficient_data` rather than being labelled on thin evidence.
- **The PPO caveat from C4 applies.** Rates come from the shared CA file, after removing rows with in-file HMO evidence.
