import argparse
import re
from collections import Counter, defaultdict

import pandas as pd

from src.common import join_sorted, load_config, log, setup_logging, write_json

NETWORK_TYPES = ("PPO", "HMO", "EPO", "POS")
SPECIAL_TOKENS = ("HSP", "EOA")
UNKNOWN = "OTHER/UNKNOWN"
TARGET_STATE = "CA"
TARGET_MARKET = "group"
TARGET_NETWORK = "PPO"
TOKEN_RE = re.compile(r"(?<![A-Za-z])(PPO|HMO|EPO|POS|HSP|EOA)(?![A-Za-z])", re.I)
HIOS_RE = re.compile(r"^\d{5}([A-Z]{2})\d")
EIN_RE = re.compile(r"^\d{9}$")
URL_STATE_RE = re.compile(r"_[a-z]+-([a-z]{2})(?=[-_.])", re.I)
NON_COMMERCIAL_RE = re.compile(r"(?<![A-Za-z])(medicare|medi-cal|medicaid|d-snp|dsnp|mapd|chip|cal mediconnect)(?![A-Za-z])", re.I)
DENTAL_VISION_ONLY_RE = re.compile(r"^\s*(dental|vision)\b|\b(dental|vision)[\s-]+(only|plan)\b|\bbehavioral health only\b", re.I)
COSTSHARE_RE = re.compile(r"(?<![\w$])\$?\d[\d,.]*[a-z%]?(?:/\$?\d[\d,.]*[a-z%]?)+", re.I)
US_STATES = set(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR".split()
)
PRECEDENCE = [
    ("P1", "plan_name_token", "Explicit single network token in the plan name (word-boundary match)", "high"),
    ("P1b", "plan_name_glued_token", "Network token glued to a known prefix (e.g. CUSPPO)", "medium"),
    ("P1x", "plan_name_special_token", "Explicit but unresolved token (EOA, HSP, PPO+EPO, multiple types): mapped only via config, never inferred from lower signals", "low"),
    ("P2", "file_token", "Network token in the in-network file name / description / URL path (all of the plan's files agree)", "high"),
    ("P3", "entity", "Legal entity (issuer / sponsor) rule from config", "medium"),
    ("P4", "family", "Product-family name validated against explicit-token plans (support and purity thresholds)", "medium"),
    ("P5", "propagation", "All of the plan's files are confidently single-type (bipartite plan-file graph)", "medium"),
    ("P6", "costshare", "Cost-share shape rule, only if validated on explicit-token plans", "low"),
]
CONF_RANK = {"high": 3, "medium": 2, "low": 1, "": 0}


def word_re(term):
    return re.compile(r"(?<![A-Za-z])" + re.escape(term).replace(r"\ ", r"\s+") + r"(?![A-Za-z])", re.I)


def glued_re(prefixes):
    if not prefixes:
        return None
    return re.compile(r"(?<![A-Za-z])(?:" + "|".join(map(re.escape, prefixes)) + r")(PPO|HMO|EPO|POS)(?![A-Za-z])", re.I)


def find_tokens(text):
    return tuple(sorted({t.upper() for t in TOKEN_RE.findall(text or "")}))


def find_glued(text, rx):
    return tuple(sorted({t.upper() for t in rx.findall(text or "")})) if rx else ()


def token_label(tokens):
    types = sorted(set(tokens) & set(NETWORK_TYPES))
    specials = sorted(set(tokens) & set(SPECIAL_TOKENS))
    if len(types) == 1 and not specials:
        return types[0]
    if types == ["EPO", "PPO"] and not specials:
        return "PPO+EPO"
    if not types and len(specials) == 1:
        return specials[0]
    if types or specials:
        return "MULTI:" + "+".join(types + specials)
    return ""


def costshare_shape(name):
    m = COSTSHARE_RE.search(name or "")
    return len(m.group(0).split("/")) if m else 0


def url_state(file_name):
    states = {m.upper() for m in URL_STATE_RE.findall(file_name or "")} & US_STATES
    return states.pop() if len(states) == 1 else ""


def hios_state(pid):
    m = HIOS_RE.match(str(pid or ""))
    return m.group(1) if m and m.group(1) in US_STATES else ""


def id_shape(pid):
    pid = str(pid or "")
    return "hios_like" if HIOS_RE.match(pid) else "ein_like" if EIN_RE.match(pid) else "other"


def brand_states(plans, min_share):
    out = {}
    for brand, g in plans.groupby("brand"):
        c = Counter(s for s in g["hios_state"] if s)
        if c:
            st, n = c.most_common(1)[0]
            if n / sum(c.values()) >= min_share:
                out[brand] = (st, n, sum(c.values()))
    return out


def load_inputs(out_dir):
    c1 = out_dir / "c1"
    long = pd.read_parquet(c1 / "plan_file_long.parquet")
    plans = pd.read_parquet(c1 / "plans.parquet")
    text_cols = ["brand", "plan_name", "plan_id", "plan_id_type", "plan_market_type", "issuer_name", "plan_sponsor_name", "file_names", "index_files"]
    plans[text_cols] = plans[text_cols].fillna("").astype(str)
    files = pd.read_parquet(c1 / "files_dedup.parquet")
    return long, plans, files


def file_signals(files):
    rows = []
    for r in files.itertuples(index=False):
        text = " ".join([r.file_name or "", r.file_description or "", r.file_url or ""])
        toks = find_tokens(re.sub(r"[_\-./]", " ", text))
        rows.append({"file_key": r.file_key, "file_state": url_state(r.file_name), "file_tokens": "+".join(toks), "file_token_label": token_label(toks)})
    return pd.DataFrame(rows)


def apply_scope(plans, plan_files, fsig, cfg2):
    fstate = dict(zip(fsig["file_key"], fsig["file_state"]))
    plans["hios_state"] = plans["plan_id"].map(hios_state)
    bstates = brand_states(plans, float(cfg2.get("brand_state_min_share", 0.9)))
    rows = []
    for r in plans.itertuples(index=False):
        ust = sorted({fstate.get(k, "") for k in plan_files.get(r.plan_key, [])} - {""})
        bst = bstates.get(r.brand, ("", 0, 0))
        if len(ust) == 1:
            state, ev = ust[0], f"url_state={ust[0]}"
        elif r.hios_state:
            state, ev = r.hios_state, f"hios_state={r.hios_state}"
        elif bst[0]:
            state, ev = bst[0], f"brand_state={bst[0]} ({bst[1]}/{bst[2]} HIOS ids of brand {r.brand})"
        else:
            state, ev = "", "no_state_evidence"
        reasons, flags = [], []
        if len(ust) > 1:
            flags.append("MULTI_STATE_FILES:" + "+".join(ust))
        if ust and r.hios_state and r.hios_state not in ust:
            flags.append(f"STATE_CONFLICT_URL_{'+'.join(ust)}_VS_HIOS_{r.hios_state}")
        if state != TARGET_STATE:
            reasons.append("OUT_OF_STATE" if state else "STATE_UNKNOWN")
        mt = (r.plan_market_type or "").lower()
        if mt != TARGET_MARKET:
            reasons.append({"individual": "INDIVIDUAL_MARKET", "": "MARKET_UNKNOWN"}.get(mt, "NON_GROUP_MARKET"))
        if NON_COMMERCIAL_RE.search(r.plan_name or ""):
            reasons.append("NON_COMMERCIAL_PRODUCT")
        if DENTAL_VISION_ONLY_RE.search(r.plan_name or ""):
            reasons.append("DENTAL_VISION_BH_ONLY")
        it, sh = (r.plan_id_type or "").lower(), id_shape(r.plan_id)
        if mt == "group" and it == "hios":
            flags.append("ID_TYPE_CONFLICT_GROUP_WITH_HIOS")
        if mt == "individual" and it == "ein":
            flags.append("ID_TYPE_CONFLICT_INDIVIDUAL_WITH_EIN")
        if it == "ein" and sh == "hios_like":
            flags.append("ID_SHAPE_CONFLICT_EIN_TYPED_HIOS_VALUE")
        if it == "hios" and sh == "ein_like":
            flags.append("ID_SHAPE_CONFLICT_HIOS_TYPED_EIN_VALUE")
        rows.append({"plan_key": r.plan_key, "state": state, "state_evidence": ev, "scope_verdict": "IN_SCOPE" if not reasons else "OUT_OF_SCOPE",
                     "scope_reason": reasons[0] if reasons else "", "scope_reasons_all": "|".join(reasons), "data_flags": "|".join(flags)})
    return plans.merge(pd.DataFrame(rows), on="plan_key"), bstates


def validate_families(df, candidates, min_support, min_purity):
    base = df[(df["scope_verdict"] == "IN_SCOPE") & df["name_label"].isin(NETWORK_TYPES)]
    rows = []
    for term in candidates:
        rx = word_re(term)
        hit = base[base["plan_name"].map(lambda n: bool(rx.search(n or "")))]
        c = Counter(hit["name_label"])
        n = sum(c.values())
        top, k = (c.most_common(1)[0] if c else ("", 0))
        purity = k / n if n else 0.0
        rows.append({"family": term, "support": n, "types": dict(c), "implied_type": top if n >= min_support and purity >= min_purity else "",
                     "purity": round(purity, 4), "validated": bool(n >= min_support and purity >= min_purity)})
    return rows


def validate_costshare(df, min_support, min_precision):
    base = df[(df["scope_verdict"] == "IN_SCOPE") & df["name_label"].isin(NETWORK_TYPES) & (df["costshare_shape"] > 0)]
    rows = []
    for shape, g in base.groupby("costshare_shape"):
        c = Counter(g["name_label"])
        top, k = c.most_common(1)[0]
        prec = k / len(g)
        rows.append({"shape": int(shape), "support": len(g), "types": dict(c), "majority": top, "precision": round(prec, 4),
                     "validated": bool(len(g) >= min_support and prec >= min_precision)})
    return rows


def family_signal(name, fams):
    implied = sorted({f["implied_type"] for f in fams if f["validated"] and word_re(f["family"]).search(name or "")})
    hits = sorted(f["family"] for f in fams if f["validated"] and word_re(f["family"]).search(name or ""))
    return ("+".join(implied) if len(implied) == 1 else ("MULTI:" + "+".join(implied) if implied else "")), "+".join(hits)


def entity_signal(issuer, sponsor, rules):
    text = f"{issuer or ''} | {sponsor or ''}"
    hits = sorted({r["implies"] for r in rules or [] if re.search(re.escape(r["pattern"]), text, re.I)})
    return hits[0] if len(hits) == 1 else ("MULTI:" + "+".join(hits) if hits else "")


def special_mapping(label, cfg2):
    if label == "EOA":
        m = (cfg2.get("eoa_as") or "unknown").upper()
        return (m, "low", "P1x_EOA_MAPPED_BY_CONFIG") if m in NETWORK_TYPES else (UNKNOWN, "low", "EOA_UNRESOLVED")
    if label == "HSP":
        m = (cfg2.get("hsp_as") or "unknown").upper()
        return (m, "low", "P1x_HSP_MAPPED_BY_CONFIG") if m in NETWORK_TYPES else (UNKNOWN, "low", "HSP_UNRESOLVED")
    if label == "PPO+EPO":
        m = (cfg2.get("ppo_epo_as") or "conflict").upper()
        return (m, "low", "P1x_PPO_EPO_MAPPED_BY_CONFIG") if m in NETWORK_TYPES else (UNKNOWN, "low", "PPO_EPO_CONFLICT")
    return UNKNOWN, "low", "MULTI_NETWORK_TOKEN_CONFLICT"


def decide(r, cfg2, use_prop=True):
    if r["name_label"] in NETWORK_TYPES:
        return r["name_label"], "high", "P1_PLAN_NAME_TOKEN"
    if r["glued_label"] in NETWORK_TYPES and not r["name_label"]:
        return r["glued_label"], "medium", "P1b_PLAN_NAME_GLUED_TOKEN"
    if r["name_label"]:
        return special_mapping(r["name_label"], cfg2)
    if r["file_signal"] in NETWORK_TYPES:
        return r["file_signal"], "high", "P2_FILE_TOKEN"
    if r["entity_signal"] in NETWORK_TYPES:
        return r["entity_signal"], "medium", "P3_LEGAL_ENTITY"
    if r["family_signal"] in NETWORK_TYPES:
        return r["family_signal"], "medium", "P4_PRODUCT_FAMILY"
    if use_prop and r.get("propagation_signal") in NETWORK_TYPES:
        return r["propagation_signal"], "medium", "P5_FILE_PROPAGATION"
    if r["costshare_signal"] in NETWORK_TYPES:
        return r["costshare_signal"], "low", "P6_COSTSHARE_SHAPE"
    return UNKNOWN, "low", "NO_NETWORK_EVIDENCE"


def propagate(df, plan_files):
    file_plans = defaultdict(list)
    for pk, fks in plan_files.items():
        for fk in fks:
            file_plans[fk].append(pk)
    first = df.set_index("plan_key")[["pre_label", "pre_conf"]].to_dict("index")
    file_conf = {}
    for fk, pks in file_plans.items():
        labs = {first[p]["pre_label"] for p in pks if p in first}
        confident = all(p in first and first[p]["pre_label"] in NETWORK_TYPES and CONF_RANK[first[p]["pre_conf"]] >= 2 for p in pks)
        file_conf[fk] = labs.pop() if confident and len(labs) == 1 else ""
    out = {}
    for pk, fks in plan_files.items():
        labs = {file_conf.get(fk, "") for fk in fks}
        out[pk] = labs.pop() if fks and len(labs) == 1 and "" not in labs else ""
    return out, file_conf


def find_conflicts(df):
    rows = []
    sig_cols = [("name_label", "strong"), ("glued_label", "strong"), ("file_signal", "strong"), ("entity_signal", "strong"),
                ("family_signal", "strong"), ("propagation_signal", "strong"), ("costshare_signal", "weak")]
    for r in df.to_dict("records"):
        sigs = {c: r[c] for c, _ in sig_cols if r.get(c)}
        typed = {c: v for c, v in sigs.items() if v in NETWORK_TYPES}
        disagree = sorted(c for c, v in typed.items() if v != r["network_label"])
        unresolved = r["decision_reason"] in ("EOA_UNRESOLVED", "HSP_UNRESOLVED", "PPO_EPO_CONFLICT", "MULTI_NETWORK_TOKEN_CONFLICT")
        if disagree or unresolved or str(r["family_signal"]).startswith("MULTI") or r["data_flags"]:
            strong = [c for c in disagree if dict(sig_cols)[c] == "strong"]
            kind = []
            if unresolved:
                kind.append("UNRESOLVED_TOKEN")
            if strong:
                kind.append("STRONG_SIGNAL_DISAGREES")
            if disagree and not strong:
                kind.append("WEAK_SIGNAL_DISAGREES")
            if str(r["family_signal"]).startswith("MULTI"):
                kind.append("FAMILY_MULTI")
            if r["data_flags"]:
                kind.append("DATA_FLAG")
            rows.append({"level": "plan", "key": r["plan_key"], "brand": r["brand"], "name": r["plan_name"], "plan_id": r["plan_id"],
                         "scope_verdict": r["scope_verdict"], "network_label": r["network_label"], "decision_reason": r["decision_reason"],
                         "conflict_kind": "|".join(kind), "disagreeing_signals": "|".join(f"{c}={typed[c]}" for c in disagree),
                         "all_signals": "|".join(f"{c}={v}" for c, v in sigs.items()), "data_flags": r["data_flags"]})
    return rows


def classify_files(files, fsig, df, plan_files, file_conf):
    by_file = defaultdict(list)
    recs = df.set_index("plan_key").to_dict("index")
    for pk, fks in plan_files.items():
        for fk in fks:
            by_file[fk].append(pk)
    rows = []
    sig = fsig.set_index("file_key").to_dict("index")
    for f in files.itertuples(index=False):
        pks = by_file.get(f.file_key, [])
        ins = [recs[p] for p in pks if recs[p]["scope_verdict"] == "IN_SCOPE"]
        pool = ins if ins else [recs[p] for p in pks]
        lab = Counter(p["network_label"] for p in pool)
        typed = sorted(t for t in lab if t in NETWORK_TYPES)
        n_unk = lab.get(UNKNOWN, 0)
        s = sig.get(f.file_key, {})
        if s.get("file_token_label") in NETWORK_TYPES and typed in ([], [s["file_token_label"]]):
            label, conf, reason = s["file_token_label"], "high", "FILE_TOKEN_CONSISTENT_WITH_PLANS"
        elif len(typed) > 1:
            label, conf, reason = "MIXED", "high", "PLANS_OF_DIFFERENT_TYPES_SHARE_FILE"
        elif len(typed) == 1 and n_unk == 0:
            label, conf, reason = typed[0], "high" if file_conf.get(f.file_key) else "medium", "ALL_PLANS_SAME_TYPE"
        elif len(typed) == 1:
            label, conf, reason = typed[0], "low", "ONE_TYPE_PLUS_UNKNOWN_PLANS"
        else:
            label, conf, reason = UNKNOWN, "low", "NO_TYPED_PLANS"
        conflicts = []
        if s.get("file_token_label") in NETWORK_TYPES and typed and typed != [s["file_token_label"]]:
            conflicts.append(f"file_token={s['file_token_label']} vs plans={'+'.join(typed)}")
        if label == "MIXED":
            conflicts.append("mixed_network_file")
        out_reasons = Counter(recs[p]["scope_reason"] for p in pks if recs[p]["scope_verdict"] != "IN_SCOPE")
        rows.append({
            "file_key": f.file_key, "file_name": f.file_name, "file_url": f.file_url, "brands": f.brands, "file_state": s.get("file_state", ""),
            "scope_verdict": "IN_SCOPE" if ins else "OUT_OF_SCOPE", "scope_reason": "" if ins else (out_reasons.most_common(1)[0][0] if out_reasons else "NO_PLANS"),
            "network_label": label, "confidence": conf, "decision_reason": reason,
            "n_plans": len(pks), "n_in_scope_plans": len(ins),
            "n_ppo_plans": sum(1 for p in ins if p["network_label"] == "PPO"),
            "n_non_ppo_plans": sum(1 for p in ins if p["network_label"] in NETWORK_TYPES and p["network_label"] != "PPO"),
            "n_unknown_plans": sum(1 for p in ins if p["network_label"] == UNKNOWN),
            "label_counts": "|".join(f"{k}={v}" for k, v in sorted(lab.items())),
            "file_tokens": s.get("file_tokens", ""), "file_token_label": s.get("file_token_label", ""),
            "propagation_label": file_conf.get(f.file_key, ""), "conflicts": "|".join(conflicts),
        })
    return pd.DataFrame(rows).sort_values(["scope_verdict", "network_label", "file_name"], kind="stable").reset_index(drop=True)


def run(cfg, fingerprint=False):
    setup_logging()
    out = cfg["output_dir"]
    c2_dir = out / "c2"
    c2_dir.mkdir(parents=True, exist_ok=True)
    cfg2 = cfg.get("c2") or {}
    long, plans, files = load_inputs(out)
    log.info("c2: loaded %d plans, %d files, %d plan-file rows", len(plans), len(files), len(long))
    plan_files = {pk: sorted(set(g.dropna().tolist())) for pk, g in long.groupby("plan_key")["file_key"]}
    grx = glued_re(cfg2.get("glued_prefixes") or [])
    fsig = file_signals(files)
    df, bstates = apply_scope(plans.copy(), plan_files, fsig, cfg2)
    log.info("c2 stage A: in-scope=%d out-of-scope=%d %s", (df.scope_verdict == "IN_SCOPE").sum(), (df.scope_verdict != "IN_SCOPE").sum(),
             dict(Counter(df.loc[df.scope_verdict != "IN_SCOPE", "scope_reason"])))
    df["name_tokens"] = df["plan_name"].map(lambda n: "+".join(find_tokens(n)))
    df["name_label"] = df["plan_name"].map(lambda n: token_label(find_tokens(n)))
    df["glued_tokens"] = df["plan_name"].map(lambda n: "+".join(find_glued(n, grx)))
    df["glued_label"] = df["plan_name"].map(lambda n: token_label(find_glued(n, grx)))
    flab = dict(zip(fsig["file_key"], fsig["file_token_label"]))
    df["file_signal"] = df["plan_key"].map(lambda pk: (lambda s: s.pop() if len(s) == 1 else "")({flab.get(k, "") for k in plan_files.get(pk, [])}) if plan_files.get(pk) else "")
    df["entity_signal"] = [entity_signal(i, s, cfg2.get("entity_rules")) for i, s in zip(df["issuer_name"], df["plan_sponsor_name"])]
    fams = validate_families(df, cfg2.get("family_candidates") or [], int(cfg2.get("family_min_support", 5)), float(cfg2.get("family_min_purity", 1.0)))
    fs = df["plan_name"].map(lambda n: family_signal(n, fams))
    df["family_signal"], df["family_hits"] = fs.map(lambda t: t[0]), fs.map(lambda t: t[1])
    df["costshare_shape"] = df["plan_name"].map(costshare_shape)
    cs_rules = validate_costshare(df, int(cfg2.get("costshare_min_support", 20)), float(cfg2.get("costshare_min_precision", 0.95)))
    cs_ok = {r["shape"]: r["majority"] for r in cs_rules if r["validated"]}
    df["costshare_signal"] = df["costshare_shape"].map(lambda s: cs_ok.get(s, ""))
    df["propagation_signal"] = ""
    pre = df.apply(lambda r: decide(r, cfg2, use_prop=False), axis=1)
    df["pre_label"], df["pre_conf"] = pre.map(lambda t: t[0]), pre.map(lambda t: t[1])
    prop, file_conf = propagate(df, plan_files)
    df["propagation_signal"] = df["plan_key"].map(lambda pk: prop.get(pk, ""))
    dec = df.apply(lambda r: decide(r, cfg2), axis=1)
    df["network_label"], df["confidence"], df["decision_reason"] = dec.map(lambda t: t[0]), dec.map(lambda t: t[1]), dec.map(lambda t: t[2])
    df["is_target_ppo"] = (df["scope_verdict"] == "IN_SCOPE") & (df["network_label"] == TARGET_NETWORK)
    df = df.drop(columns=["pre_label", "pre_conf"])
    df["final_reason_code"] = df.apply(lambda r: r["scope_reason"] if r["scope_verdict"] != "IN_SCOPE" else (
        "PPO" if r["is_target_ppo"] else {"HMO": "HMO", "EPO": "EPO", "POS": "POS"}.get(r["network_label"], r["decision_reason"])), axis=1)
    df["file_keys"] = df["plan_key"].map(lambda pk: "|".join(plan_files.get(pk, [])))
    fcls = classify_files(files, fsig, df, plan_files, file_conf)
    if fingerprint:
        from src import c2_fingerprint
        fp = c2_fingerprint.run(cfg, fcls)
        fcls = fcls.merge(fp, on="file_key", how="left") if fp is not None and len(fp) else fcls
    conflicts = find_conflicts(df)
    for r in fcls[fcls["conflicts"] != ""].to_dict("records"):
        conflicts.append({"level": "file", "key": r["file_key"], "brand": r["brands"], "name": r["file_name"], "plan_id": "",
                          "scope_verdict": r["scope_verdict"], "network_label": r["network_label"], "decision_reason": r["decision_reason"],
                          "conflict_kind": "FILE_" + r["decision_reason"], "disagreeing_signals": r["conflicts"], "all_signals": r["label_counts"], "data_flags": ""})
    conf_df = pd.DataFrame(conflicts).sort_values(["level", "scope_verdict", "conflict_kind", "name", "key"], kind="stable")
    plan_cols = ["plan_key", "brand", "plan_name", "plan_id", "plan_id_type", "plan_market_type", "issuer_name", "plan_sponsor_name",
                 "state", "state_evidence", "scope_verdict", "scope_reason", "scope_reasons_all", "data_flags",
                 "name_tokens", "name_label", "glued_tokens", "glued_label", "file_signal", "entity_signal", "family_hits", "family_signal",
                 "propagation_signal", "costshare_shape", "costshare_signal", "network_label", "confidence", "decision_reason",
                 "final_reason_code", "is_target_ppo", "n_files", "file_keys", "file_names", "index_files"]
    pc = df[plan_cols].sort_values(["scope_verdict", "network_label", "brand", "plan_name", "plan_id", "plan_key"], kind="stable")
    pc.to_csv(c2_dir / "plan_classification.csv", index=False)
    fcls.to_csv(c2_dir / "file_classification.csv", index=False)
    ppo_plans = pc[pc["is_target_ppo"]]
    ppo_plans[["plan_key", "plan_name", "plan_id", "plan_id_type", "confidence", "decision_reason", "file_keys", "file_names"]].to_csv(c2_dir / "ppo_plans.csv", index=False)
    ppo_files = fcls[(fcls["scope_verdict"] == "IN_SCOPE") & (fcls["n_ppo_plans"] > 0)].copy()
    ppo_files["ppo_plan_share"] = (ppo_files["n_ppo_plans"] / ppo_files["n_in_scope_plans"]).round(4)
    ppo_files["requires_in_file_network_filter"] = ppo_files["network_label"] != TARGET_NETWORK
    ppo_files.to_csv(c2_dir / "ppo_files.csv", index=False)
    conf_df.to_csv(c2_dir / "conflicts.csv", index=False)
    excl = []
    for code, n in Counter(pc["final_reason_code"]).items():
        excl.append({"level": "plan", "reason_code": code, "count": n, "kept": code == "PPO"})
    for code, n in Counter(r for rs in pc["scope_reasons_all"] for r in rs.split("|") if r).items():
        excl.append({"level": "plan_any_scope_reason", "reason_code": code, "count": n, "kept": False})
    for code, n in Counter(fcls.apply(lambda r: r["scope_reason"] if r["scope_verdict"] != "IN_SCOPE" else ("PPO" if r["network_label"] == "PPO" else ("MIXED_CONTAINS_PPO" if r["n_ppo_plans"] else r["network_label"])), axis=1)).items():
        excl.append({"level": "file", "reason_code": code, "count": n, "kept": code in ("PPO", "MIXED_CONTAINS_PPO")})
    pd.DataFrame(excl).sort_values(["level", "count", "reason_code"], ascending=[True, False, True]).to_csv(c2_dir / "exclusions_summary.csv", index=False)
    ins = pc[pc["scope_verdict"] == "IN_SCOPE"]
    eoa_like = ins[ins["decision_reason"].isin(["EOA_UNRESOLVED", "PPO_EPO_CONFLICT", "HSP_UNRESOLVED", "MULTI_NETWORK_TOKEN_CONFLICT", "NO_NETWORK_EVIDENCE"])]
    stats = {
        "config": {k: cfg2.get(k) for k in ("eoa_as", "ppo_epo_as", "hsp_as", "glued_prefixes")},
        "plans_total": int(len(pc)), "files_total": int(len(fcls)),
        "in_scope_plans": int(len(ins)), "out_of_scope_plans_by_primary_reason": dict(Counter(pc.loc[pc.scope_verdict != "IN_SCOPE", "scope_reason"])),
        "in_scope_by_label": dict(Counter(ins["network_label"])), "in_scope_by_decision_reason": dict(Counter(ins["decision_reason"])),
        "in_scope_by_confidence": dict(Counter(ins["confidence"])),
        "ppo_plans": int(len(ppo_plans)), "ppo_plans_by_confidence": dict(Counter(ppo_plans["confidence"])),
        "ppo_plans_distinct_names": int(ppo_plans["plan_name"].nunique()),
        "ppo_upper_bound_if_unresolved_were_ppo": int(len(ppo_plans) + len(eoa_like)),
        "ppo_files": int(len(ppo_files)), "ppo_files_labels": dict(Counter(ppo_files["network_label"])),
        "in_scope_files": int((fcls.scope_verdict == "IN_SCOPE").sum()), "files_by_label": dict(Counter(fcls["network_label"])),
        "conflicts_rows": int(len(conf_df)), "conflicts_by_kind": dict(Counter(k for ks in conf_df["conflict_kind"] for k in ks.split("|"))),
        "conflicts_in_scope_rows": int((conf_df["scope_verdict"] == "IN_SCOPE").sum()),
        "data_flags": dict(Counter(f for fl in pc["data_flags"] for f in fl.split("|") if f)),
        "brand_state_inference": {b: {"state": s, "support": n, "hios_ids": t} for b, (s, n, t) in bstates.items()},
        "family_validation": fams, "costshare_validation": cs_rules,
        "entity_rules_hits": {r["pattern"]: int(sum(bool(re.search(re.escape(r["pattern"]), f"{i} {s}", re.I)) for i, s in zip(pc["issuer_name"], pc["plan_sponsor_name"]))) for r in cfg2.get("entity_rules") or []},
        "precedence": [{"rank": a, "signal": b, "rule": c, "confidence": d} for a, b, c, d in PRECEDENCE],
    }
    write_json(stats, c2_dir / "c2_stats.json")
    log.info("c2: in_scope=%d labels=%s ppo_plans=%d ppo_files=%d conflicts=%d", len(ins), stats["in_scope_by_label"], len(ppo_plans), len(ppo_files), len(conf_df))
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--output-dir")
    ap.add_argument("--fingerprint", action="store_true")
    a = ap.parse_args(argv)
    run(load_config(a.config, {"output_dir": a.output_dir}), fingerprint=a.fingerprint)


if __name__ == "__main__":
    main()
