"""Builds outputs/ui/index.html: one self-contained page (no server, no external scripts) with
- an executive view: network funnel, file quality grade and metrics, address linkage, cost-effectiveness mix;
- an analyst view: a searchable, filterable provider table with each provider's address, status, cost index and its most
  out-of-line services against similar providers."""
import json

import numpy as np
import pandas as pd

from src.common import log


def load_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def clean_records(df):
    return json.loads(df.replace({np.nan: None}).to_json(orient="records"))


def build_data(cfg):
    o = cfg["output_dir"]
    summary = load_json(o / "summary.json")
    c2 = load_json(o / "c2" / "c2_stats.json")
    c5 = load_json(o / "c5" / "c5_stats.json")
    c7 = load_json(o / "c7" / "c7_stats.json")
    metrics = pd.read_csv(o / "c3" / "metric_values.csv") if (o / "c3" / "metric_values.csv").exists() else pd.DataFrame()
    scores = pd.read_csv(o / "c3" / "file_scores.csv") if (o / "c3" / "file_scores.csv").exists() else pd.DataFrame()
    loc = pd.read_csv(o / "c5" / "provider_locations.csv") if (o / "c5" / "provider_locations.csv").exists() else pd.DataFrame()
    prov = pd.read_csv(o / "c7" / "provider_cost_effectiveness.csv", dtype={"tin": str}) if (o / "c7" / "provider_cost_effectiveness.csv").exists() else pd.DataFrame()
    services = {}
    comp_path = o / "c7" / "service_comparisons.parquet"
    if comp_path.exists():
        comp = pd.read_parquet(comp_path, columns=["npi", "tin", "service_label", "billing_code", "rate", "peer_median", "price_ratio", "n_peers", "peer_level"])
        comp["dev"] = np.abs(np.log(comp["price_ratio"]))
        top = comp.sort_values("dev", ascending=False).groupby(["npi", "tin"]).head(6)
        for (npi, tin), g in top.groupby(["npi", "tin"]):
            services[f"{npi}|{tin}"] = clean_records(g.drop(columns="dev").round({"rate": 2, "peer_median": 2, "price_ratio": 3}))
    if len(prov) and len(loc):
        prov = prov.merge(loc[["npi", "best_address", "best_confidence", "best_sources"]], on="npi", how="left")
    table = clean_records(prov[[c for c in ["npi", "tin", "provider_name", "entity_type", "specialty", "county", "address_status", "best_address",
                                            "best_sources", "cost_index", "median_percentile", "n_services_compared", "n_services_priced", "label"]
                                if c in prov]]) if len(prov) else []
    return {
        "summary": summary,
        "funnel": [["Unique plans", summary.get("unique_plans")], ["In-scope CA group plans", summary.get("in_scope_ca_group_plans")],
                   ["PPO plans", summary.get("ppo_plans")], ["PPO rate files", summary.get("ppo_rate_files")]],
        "in_scope_by_label": c2.get("in_scope_by_label", {}),
        "file_scores": clean_records(scores[["file_name", "total_score", "grade"]]) if len(scores) else [],
        "metrics": clean_records(metrics[["metric", "group", "raw_value", "score", "weight", "definition"]]) if len(metrics) else [],
        "location_status": c5.get("npis_by_location_status", {}),
        "cost_labels": c7.get("by_label", {}),
        "providers": table, "services": services,
    }


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PPO Rate Explorer</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e2dc;--accent:#2f6f8f;--good:#2e7d4f;--warn:#b26b00;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--panel:#1f1f1d;--ink:#ecebe6;--muted:#a3a29b;--line:#34332f;--accent:#6fb1d1;--good:#6cc38f;--warn:#e0a54a;--bad:#ef7d74}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:20px 16px 8px;max-width:1200px;margin:auto}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 10px}
.sub{color:var(--muted)}nav{display:flex;gap:8px;margin-top:12px}nav button{border:1px solid var(--line);background:var(--panel);color:var(--ink);
padding:6px 14px;border-radius:999px;cursor:pointer}nav button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
main{max-width:1200px;margin:auto;padding:8px 16px 40px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}.kpi{font-size:26px;font-weight:600}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{cursor:pointer;color:var(--muted);font-weight:500;position:sticky;top:0;background:var(--panel)}tr.row:hover{background:var(--bg);cursor:pointer}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;border:1px solid var(--line)}
.cost_effective{color:var(--good)}.high_cost{color:var(--bad)}.in_line{color:var(--accent)}.insufficient_data{color:var(--muted)}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}input,select{padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink)}
.scroll{overflow:auto;max-height:70vh}.detail td{background:var(--bg)}.hidden{display:none}.num{text-align:right;font-variant-numeric:tabular-nums}
</style></head><body>
<header><h1>Centene CA PPO: rate file explorer</h1><div class="sub" id="asof"></div>
<nav><button id="b-exec" class="on">Executive view</button><button id="b-analyst">Analyst view</button></nav></header>
<main>
<section id="exec">
 <div class="grid" id="kpis"></div><br>
 <div class="grid">
  <div class="card"><h2>Network funnel (C1–C2)</h2><div id="funnel"></div></div>
  <div class="card"><h2>Rate file quality (C3)</h2><div id="filescore"></div><div id="metrics"></div></div>
  <div class="card"><h2>NPI address status (C5)</h2><div id="locs"></div></div>
  <div class="card"><h2>Cost effectiveness (C7)</h2><div id="costs"></div></div>
 </div>
</section>
<section id="analyst" class="hidden">
 <div class="filters"><input id="q" placeholder="Search name, NPI, TIN, address, specialty" size="40">
  <select id="fl"><option value="">All cost labels</option></select><select id="fs"><option value="">All address statuses</option></select>
  <span class="sub" id="count"></span></div>
 <div class="card scroll"><table><thead><tr id="hdr"></tr></thead><tbody id="rows"></tbody></table></div>
 <p class="sub">Click a provider to see its most out-of-line services against similar providers (same service, same specialty or facility type, same county; state when the county has fewer than 5 peers).</p>
</section>
</main>
<script>
const D = __DATA__;
const $ = id => document.getElementById(id), fmt = (v, d=0) => v == null ? "–" : Number(v).toLocaleString(undefined,{maximumFractionDigits:d,minimumFractionDigits:d});
const pct = v => v == null ? "–" : (100*v).toFixed(1) + "%";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const S = D.summary;
$("asof").textContent = "Data as of " + (S.as_of_date || "?") + " · generated by python -m src.run --stage ui";
const kp = [["NPIs verified", pct(S.share_npis_verified_address), "two identifier-linked sources or CA licence register"],
            ["NPIs verified or corroborated", pct(S.share_npis_verified_or_corroborated_address), "adds NPPES + name/ZIP-matched site"],
            ["TINs verified", pct(S.share_tins_verified_address), "any NPI under the TIN · " + pct(S.share_tins_verified_or_corroborated_address) + " incl. corroborated"],
            ["PPO plans", fmt(S.ppo_plans), "from " + fmt(S.in_scope_ca_group_plans) + " in-scope CA group plans"],
            ["Selected rates", fmt(S.selected_rates_npi_tin_code_context), fmt(S.unique_npis) + " NPIs · " + fmt(S.unique_tins) + " TINs"],
            ["Rate file rows", fmt(S.rate_file_rows_npi_tin_address_code), "one per NPI × TIN × address × billing code"]];
$("kpis").innerHTML = kp.map(k => `<div class="card"><div class="sub">${k[0]}</div><div class="kpi">${k[1]}</div><div class="sub">${esc(k[2])}</div></div>`).join("");
const bars = (pairs, total) => pairs.map(([k, v]) => `<div style="margin:6px 0"><div style="display:flex;justify-content:space-between"><span>${esc(k)}</span><span class="num">${fmt(v)}</span></div><div class="bar"><i style="width:${total ? 100*v/total : 0}%"></i></div></div>`).join("");
$("funnel").innerHTML = bars(D.funnel, D.funnel[0][1]) + `<p class="sub">In scope by network: ${Object.entries(D.in_scope_by_label).map(([k,v]) => esc(k)+" "+v).join(" · ")}</p>`;
$("filescore").innerHTML = D.file_scores.map(f => `<p><b>Grade ${esc(f.grade)}</b> (${fmt(f.total_score,3)}) · <span class="sub">${esc(f.file_name)}</span></p>`).join("");
$("metrics").innerHTML = bars(D.metrics.map(m => [m.metric.replaceAll("_"," "), m.score == null ? 0 : m.score*100]), 100).replaceAll(/>(\\d+)<\\/span>/g, ">$1%</span>");
const ls = Object.entries(D.location_status).sort((a,b) => b[1]-a[1]); $("locs").innerHTML = bars(ls, ls.reduce((a,b) => a+b[1], 0));
const cl = Object.entries(D.cost_labels).sort((a,b) => b[1]-a[1]); $("costs").innerHTML = bars(cl.map(([k,v]) => [k.replaceAll("_"," "), v]), cl.reduce((a,b) => a+b[1], 0));
const cols = [["provider_name","Provider"],["npi","NPI"],["tin","TIN"],["specialty","Specialty"],["county","County"],["address_status","Address status"],
              ["best_address","Best address"],["cost_index","Cost index"],["n_services_compared","Services compared"],["label","Cost label"]];
$("hdr").innerHTML = cols.map(([k,l]) => `<th data-k="${k}">${l}</th>`).join("");
const uniq = k => [...new Set(D.providers.map(p => p[k]).filter(Boolean))].sort();
uniq("label").forEach(v => $("fl").insertAdjacentHTML("beforeend", `<option>${esc(v)}</option>`));
uniq("address_status").forEach(v => $("fs").insertAdjacentHTML("beforeend", `<option>${esc(v)}</option>`));
let sortK = "cost_index", sortDir = 1, open = null;
function render(){
  const q = $("q").value.toLowerCase(), fl = $("fl").value, fs = $("fs").value;
  let r = D.providers.filter(p => (!fl || p.label === fl) && (!fs || p.address_status === fs) &&
     (!q || [p.provider_name,p.npi,p.tin,p.best_address,p.specialty,p.county].join(" ").toLowerCase().includes(q)));
  r.sort((a,b) => { const x=a[sortK], y=b[sortK]; return (x==null)-(y==null) || (x<y?-1:x>y?1:0)*sortDir; });
  $("count").textContent = fmt(r.length) + " providers" + (r.length > 500 ? " (first 500 shown)" : "");
  $("rows").innerHTML = r.slice(0,500).map(p => {
    const key = p.npi + "|" + (p.tin ?? ""), svc = D.services[key] || [];
    const main = `<tr class="row" data-key="${esc(key)}">${cols.map(([k]) => `<td class="${k==="cost_index"||k==="n_services_compared"?"num":""}">${
      k==="label" ? `<span class="tag ${esc(p.label)}">${esc((p.label||"").replaceAll("_"," "))}</span>` : k==="cost_index" ? fmt(p[k],2) : esc(p[k])}</td>`).join("")}</tr>`;
    const det = open === key ? `<tr class="detail"><td colspan="${cols.length}">${svc.length ? `<table><tr><th>Service</th><th>Code</th><th class="num">Rate</th><th class="num">Peer median</th><th class="num">Ratio</th><th class="num">Peers</th><th>Peer group</th></tr>${
      svc.map(s => `<tr><td>${esc(s.service_label)}</td><td>${esc(s.billing_code)}</td><td class="num">${fmt(s.rate,2)}</td><td class="num">${fmt(s.peer_median,2)}</td><td class="num">${fmt(s.price_ratio,2)}</td><td class="num">${fmt(s.n_peers)}</td><td>${esc(s.peer_level)}</td></tr>`).join("")}</table>` : '<span class="sub">No service had enough similar providers to compare.</span>'}<p class="sub">Address sources: ${esc(p.best_sources)}</p></td></tr>` : "";
    return main + det; }).join("");
}
$("rows").addEventListener("click", e => { const tr = e.target.closest("tr.row"); if (tr) { open = open === tr.dataset.key ? null : tr.dataset.key; render(); } });
$("hdr").addEventListener("click", e => { const k = e.target.dataset.k; if (!k) return; sortDir = sortK === k ? -sortDir : 1; sortK = k; render(); });
["q","fl","fs"].forEach(id => $(id).addEventListener("input", render));
const show = v => { $("exec").classList.toggle("hidden", v !== "exec"); $("analyst").classList.toggle("hidden", v !== "analyst");
  $("b-exec").classList.toggle("on", v === "exec"); $("b-analyst").classList.toggle("on", v === "analyst"); if (v === "analyst") render(); };
$("b-exec").onclick = () => show("exec"); $("b-analyst").onclick = () => show("analyst");
</script></body></html>
"""


def run(cfg):
    out = cfg["output_dir"] / "ui"
    out.mkdir(parents=True, exist_ok=True)
    data = build_data(cfg)
    html = PAGE.replace("__DATA__", json.dumps(data, separators=(",", ":"), default=str).replace("</", "<\\/"))
    (out / "index.html").write_text(html)
    log.info("ui: wrote %s (%.1f MB, %d providers)", out / "index.html", len(html) / 1e6, len(data["providers"]))
    return {"providers": len(data["providers"]), "bytes": len(html)}
