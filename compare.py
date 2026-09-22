#!/usr/bin/env python
"""Compare two scored runs: what improved, what got worse, and where.

Both runs must already have been scored by kge_map.py. This reads their outputs
and writes ONE self-contained HTML file -- no server, no Python needed to view
it, the same deal as build_webapp.py.

    python compare.py --a outputs_v2 --b outputs_v3 --vpu 714
    python compare.py --a outputs_v2 --b outputs_v3 --vpu 714 --out cmp.html

WHAT IT COMPARES, AND WHY IT IS BUILT THIS WAY
----------------------------------------------
Three sections, in decreasing order of how much the data can support:

  1. Grouped summary -- every grouping the runs carry (overall, stream order,
     month, and any --group-by catalog columns) x every metric, both runs side
     by side with the change. This is where "which got worse and in which ways"
     is answered, and it is the best-powered object here: ~1,700 paired gauges
     split across six stream orders is ~280 a group.

  2. Decision transitions -- a matrix per decision. A verdict moving Weak ->
     Good is not a delta, it is a move between categories, and counting those
     moves says more than any average of them.

  3. Per-gauge detail -- the tail of the distribution, the gauges that moved
     most in each direction. NOT a map: a delta map is the one object here that
     works at per-gauge resolution, and whether it is trustworthy depends on how
     large the real differences turn out to be. Deferred until we have looked.

The comparison is PAIRED: same gauge, same observations, same days, only the
model differs. That matters -- sampling noise is largely shared between the two
estimates and cancels in the difference, so a paired delta is far better
determined than the gap between two independent estimates would be.

"IMPROVED" IS NOT "WENT UP"
---------------------------
KGE' is better higher, PBIAS is better closer to ZERO, FAR is better lower, and
alpha/beta/gamma are best at 1. One rule covers all of them: closer to the
metric's optimum is better. Every metric below carries its optimum, and nothing
is differenced without it.

WHAT IT REFUSES
---------------
Two runs scored over different windows are not compared. A "difference" between
them could be the years rather than the models, and nothing on the page would
say so. serve.py already refuses the same thing when merging decisions.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys

import numpy as np
import pandas as pd

# Metric, display name, and the value it is trying to reach. "Improved" is
# always |new - opt| < |old - opt|, which handles higher-is-better,
# lower-is-better and closer-to-one without special cases.
METRIC_SPECS = [
    ("kge_2012", "KGE' (Kling 2012)", 1.0, 2),
    ("nse", "NSE", 1.0, 2),
    ("r", "Pearson r", 1.0, 2),
    ("spearman", "Spearman rho", 1.0, 2),
    ("ss_clim", "Skill vs climatology", 1.0, 2),
    ("alpha", "Alpha (sigma ratio)", 1.0, 2),
    ("beta", "Beta (bias ratio)", 1.0, 2),
    ("gamma", "Gamma (CV ratio)", 1.0, 2),
    ("pbias_pct", "PBIAS (%)", 0.0, 1),
    ("nrmse", "NRMSE (/ mean flow)", 0.0, 2),
    ("mae_rel", "MAE / mean flow", 0.0, 2),
    ("pod", "POD, 2-year floods", 1.0, 2),
    ("far", "FAR, 2-year floods", 0.0, 2),
    ("csi", "CSI, 2-year floods", 1.0, 2),
    ("ets", "ETS, 2-year floods", 1.0, 2),
]

# Decision verdict columns, their display name, and the labels for each code.
# Ordered worst-to-best so a transition matrix reads bottom-left to top-right.
DECISION_SPECS = [
    ("hs_verdict", "Severe low flow",
     {-1: "Can't say", 0: "No", 1: "Weakly", 2: "Mostly", 3: "Yes"}),
    ("fl_verdict", "Floods",
     {-1: "Can't say", 0: "Poor", 1: "Weak", 2: "Good", 3: "Strong"}),
    ("vol_verdict", "Volume of water",
     {-1: "Can't say", 0: "Unsatisfactory", 1: "Satisfactory",
      2: "Good", 3: "Very good"}),
    ("tr_verdict", "Wetter or drier",
     {-1: "Can't say", 0: "Invents one", 1: "Misses it",
      2: "Neither", 3: "Agrees"}),
]

MIN_GROUP = 15          # a group with fewer paired gauges than this is not shown


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_run(outdir: str, vpu: int) -> dict:
    """One run's metrics, decisions and config."""
    mp = os.path.join(outdir, f"vpu{vpu}_metrics.parquet")
    if not os.path.exists(mp):
        sys.exit(f"no metrics parquet at {mp}\n"
                 f"  score that run first: python kge_map.py --vpu {vpu} "
                 f"--outdir {outdir}")
    cfg_path = os.path.join(outdir, f"vpu{vpu}_run.json")
    cfg = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    m = pd.read_parquet(mp)
    dp = os.path.join(outdir, f"vpu{vpu}_decisions.parquet")
    d = pd.read_parquet(dp) if os.path.exists(dp) else None
    return {"dir": outdir, "metrics": m, "decisions": d, "cfg": cfg,
            "label": cfg.get("label") or os.path.basename(outdir.rstrip("/"))}


def check_windows(a: dict, b: dict) -> None:
    """Refuse two runs scored over different periods.

    A difference between them would then be partly the years rather than the
    models, and no amount of labelling on the page makes that safe to read.
    """
    ka = (a["cfg"].get("date_start"), a["cfg"].get("date_end"))
    kb = (b["cfg"].get("date_start"), b["cfg"].get("date_end"))
    if None in ka or None in kb:
        print("  WARNING: one run has no window recorded; cannot verify they match")
        return
    if ka != kb:
        sys.exit(
            f"\nThese runs cover different windows, so a difference between them\n"
            f"would be partly the years and not the models:\n"
            f"  {a['label']}: {ka[0]} .. {ka[1]}\n"
            f"  {b['label']}: {kb[0]} .. {kb[1]}\n\n"
            f"Re-score both over one window, e.g. their overlap:\n"
            f"  python kge_map.py --vpu <VPU> --start <s> --end <e> --outdir ...")


def intersect(a: dict, b: dict) -> pd.DataFrame:
    """Pair the two runs on reach id, and say what fell out of each side.

    Two models built on different hydrofabrics do not score identical gauges.
    If the reaches one of them lacks are systematically the hard ones, comparing
    the two populations flatters it for a reason unrelated to the model -- so
    only the intersection is compared, and the losses are reported.
    """
    ma, mb = a["metrics"], b["metrics"]
    ia = set(ma.final_river_id.astype("int64"))
    ib = set(mb.final_river_id.astype("int64"))
    both = ia & ib
    print(f"      {a['label']}: {len(ia):,} gauges, {len(ia-ib):,} not in the other")
    print(f"      {b['label']}: {len(ib):,} gauges, {len(ib-ia):,} not in the other")
    print(f"      comparing the {len(both):,} present in both")
    if not both:
        sys.exit("the two runs share no gauges; nothing to compare")

    keep_a = ma[ma.final_river_id.astype("int64").isin(both)].copy()
    keep_b = mb[mb.final_river_id.astype("int64").isin(both)].copy()
    # Decision columns are dropped from the METRIC frames before merging. A
    # metrics parquet written before the decision/statistic split was fixed
    # still carries fl_* and vol_*, and leaving them in means the metric merge
    # claims those names first -- the decision merge then collides with itself
    # and pandas renames the columns out from under the lookup, so the
    # transitions silently go missing rather than failing.
    dec_pref = tuple({c.split("_")[0] + "_" for c, _, _ in DECISION_SPECS})
    for f in (keep_a, keep_b):
        f["final_river_id"] = f.final_river_id.astype("int64")
    keep_a = keep_a[[c for c in keep_a.columns if not c.startswith(dec_pref)]]
    keep_b = keep_b[[c for c in keep_b.columns if not c.startswith(dec_pref)]]
    j = keep_a.merge(keep_b, on="final_river_id", suffixes=("_a", "_b"))
    # Decisions ride along on whichever side has them.
    for run, suf in ((a, "_a"), (b, "_b")):
        if run["decisions"] is None:
            continue
        cols = ["final_river_id"] + [c for c, _, _ in DECISION_SPECS
                                     if c in run["decisions"].columns]
        dd = run["decisions"][cols].copy()
        dd["final_river_id"] = dd.final_river_id.astype("int64")
        dd = dd.rename(columns={c: c + suf for c in cols if c != "final_river_id"})
        j = j.merge(dd, on="final_river_id", how="left")
    j.attrs["n_a"], j.attrs["n_b"] = len(ia), len(ib)
    j.attrs["lost_a"], j.attrs["lost_b"] = len(ia - ib), len(ib - ia)
    return j


# --------------------------------------------------------------------------- #
# Comparing
# --------------------------------------------------------------------------- #

def improved(old: pd.Series, new: pd.Series, opt: float) -> pd.Series:
    """True where `new` sits closer to the metric's optimum than `old`."""
    return (new - opt).abs() < (old - opt).abs()


def metric_rows(j: pd.DataFrame, groups: list[tuple[str, str]]) -> list[dict]:
    """One row per grouping x group x metric, both runs and the change."""
    out = []
    for gkey, gname in groups:
        if gkey == "overall":
            buckets = [("All gauges", pd.Series(True, index=j.index))]
        else:
            col = gkey + "_a" if gkey + "_a" in j.columns else gkey
            if col not in j.columns:
                continue
            vals = j[col].dropna().unique()
            try:
                vals = sorted(vals, key=lambda v: float(v))
            except (TypeError, ValueError):
                vals = sorted(vals, key=str)
            buckets = [(str(v), j[col] == v) for v in vals]
        for bname, sel in buckets:
            sub = j[sel]
            if len(sub) < MIN_GROUP:
                continue
            for mkey, mname, opt, dec in METRIC_SPECS:
                ca, cb = mkey + "_a", mkey + "_b"
                if ca not in sub.columns or cb not in sub.columns:
                    continue
                pair = sub[[ca, cb]].replace([np.inf, -np.inf], np.nan).dropna()
                if len(pair) < MIN_GROUP:
                    continue
                imp = improved(pair[ca], pair[cb], opt)
                out.append({
                    "grouping": gname, "group": bname, "metric": mname,
                    "n": int(len(pair)),
                    "a": float(pair[ca].median()), "b": float(pair[cb].median()),
                    "opt": opt, "dec": dec,
                    "share_improved": float(imp.mean()),
                })
    return out


def transition_rows(j: pd.DataFrame) -> list[dict]:
    """Verdict-to-verdict counts per decision."""
    out = []
    for key, name, labels in DECISION_SPECS:
        ca, cb = key + "_a", key + "_b"
        if ca not in j.columns or cb not in j.columns:
            continue
        pair = j[[ca, cb]].dropna()
        if pair.empty:
            continue
        codes = sorted(labels)
        mat = [[int(((pair[ca] == r) & (pair[cb] == c)).sum()) for c in codes]
               for r in codes]
        same = sum(mat[i][i] for i in range(len(codes)))
        better = sum(mat[i][k] for i in range(len(codes))
                     for k in range(len(codes)) if k > i)
        worse = sum(mat[i][k] for i in range(len(codes))
                    for k in range(len(codes)) if k < i)
        out.append({"name": name, "labels": [labels[c] for c in codes],
                    "matrix": mat, "n": int(len(pair)),
                    "same": same, "better": better, "worse": worse})
    return out


def decision_winners(j: pd.DataFrame) -> dict:
    """Which run reached the better verdict, per decision.

    Verdict codes are ordered worst-to-best, so "better" is simply the higher
    code. A gauge the run could not judge (-1) is not a worse verdict, it is no
    verdict -- so a pair where either side is -1 is reported as unscored rather
    than counted as a win for whichever side managed one.
    """
    out = {}
    for key, name, labels in DECISION_SPECS:
        ca, cb = key + "_a", key + "_b"
        if ca not in j.columns or cb not in j.columns:
            continue
        va, vb = j[ca], j[cb]
        ok = va.notna() & vb.notna() & (va >= 0) & (vb >= 0)
        code = pd.Series(-1, index=j.index, dtype="int8")
        code[ok & (va > vb)] = 1
        code[ok & (vb > va)] = 2
        code[ok & (va == vb)] = 0
        out["D_" + key] = {"name": "Decision: " + name, "code": code.tolist(),
                           "a": int((code == 1).sum()), "b": int((code == 2).sum()),
                           "eq": int((code == 0).sum()),
                           "only": int((code == -1).sum()), "tol": 0.0}
    return out


def winners(j: pd.DataFrame, equal_frac: float = 0.02) -> dict:
    """Per gauge and per metric, which run sat closer to the optimum.

    The map colours by WHICH RUN WON, not by how much. That is deliberate: the
    size of a per-gauge difference is poorly determined, but its SIGN survives,
    because the comparison is paired -- same gauge, same observations, same
    days, so the sampling noise is largely shared and cancels.

    "About equal" is a band, because a difference of 1e-9 always has a sign and
    colouring it would be pure noise. The band is a fraction of the spread of
    that metric across gauges, so it adapts to the metric rather than being one
    number applied to KGE' and PBIAS alike.

    Returns per-metric arrays of codes: 1 A better, 2 B better, 0 about equal,
    -1 scored by only one of the runs.
    """
    out = {}
    for mkey, mname, opt, dec in METRIC_SPECS:
        ca, cb = mkey + "_a", mkey + "_b"
        if ca not in j.columns or cb not in j.columns:
            continue
        va = j[ca].replace([np.inf, -np.inf], np.nan)
        vb = j[cb].replace([np.inf, -np.inf], np.nan)
        da, db = (va - opt).abs(), (vb - opt).abs()
        both_ok = da.notna() & db.notna()
        # Scale from the INTERQUARTILE range of the values, not from the spread
        # of |value - optimum|. Metrics like KGE' are unbounded below, so the
        # latter is dominated by a handful of terrible gauges and the resulting
        # band swallows every real difference -- measured: it put 1,951 of 1,951
        # gauges in "about equal" on two runs that genuinely differ.
        pooled = pd.concat([va, vb]).dropna()
        iqr = float(pooled.quantile(.75) - pooled.quantile(.25)) if len(pooled) else 0.0
        tol = max(iqr * equal_frac, 1e-9) if np.isfinite(iqr) else 1e-9
        code = pd.Series(-1, index=j.index, dtype="int8")
        diff = db - da                       # negative: b closer to optimum
        code[both_ok & (diff.abs() <= tol)] = 0
        code[both_ok & (diff < -tol)] = 2
        code[both_ok & (diff > tol)] = 1
        n_a = int((code == 1).sum()); n_b = int((code == 2).sum())
        out[mkey] = {"name": mname, "code": code.tolist(),
                     "a": n_a, "b": n_b,
                     "eq": int((code == 0).sum()),
                     "only": int((code == -1).sum()), "tol": float(tol)}
    return out


def movers(j: pd.DataFrame, mkey: str, opt: float, n: int = 12) -> dict:
    """The gauges that moved furthest toward and away from the optimum."""
    ca, cb = mkey + "_a", mkey + "_b"
    if ca not in j.columns or cb not in j.columns:
        return {}
    d = j[["final_river_id", "gauge_id_a", ca, cb]].replace(
        [np.inf, -np.inf], np.nan).dropna()
    if d.empty:
        return {}
    d = d.assign(gain=(d[ca] - opt).abs() - (d[cb] - opt).abs())
    top = d.nlargest(n, "gain"); bot = d.nsmallest(n, "gain")
    fmt = lambda f: [{"g": str(r.gauge_id_a), "a": float(getattr(r, ca)),
                      "b": float(getattr(r, cb)), "gain": float(r.gain)}
                     for r in f.itertuples(index=False)]
    return {"better": fmt(top), "worse": fmt(bot)}


def map_payload(a: dict, b: dict, j: pd.DataFrame, win: dict, vpu: int) -> dict:
    """Points for the map: every gauge either run scored, with a code per metric.

    The union, not the intersection. Gauges present in only one run are the
    thing a comparison most easily hides -- on v2 against the v3 sample that is
    ~17% of them -- so they are drawn in their own colour rather than omitted.
    """
    ids = j.final_river_id.astype("int64").to_numpy()
    pos = {int(r): i for i, r in enumerate(ids)}
    pts, codes = [], {k: [] for k in win}
    for run in (a, b):
        m = run["metrics"]
        for r in m.itertuples(index=False):
            rid = int(r.final_river_id)
            if rid in pos:
                if run is b:
                    continue                    # already added from a
                i = pos[rid]
                for k in win:
                    codes[k].append(int(win[k]["code"][i]))
            else:
                for k in win:
                    codes[k].append(-1)
            pts.append([round(float(r.longitude), 3), round(float(r.latitude), 3),
                        str(r.gauge_id)])
    base = {}
    bp = os.path.join("cache", f"basemap_v2_vpu{vpu}.json")
    if os.path.exists(bp):
        with open(bp, encoding="utf-8") as fh:
            base = json.load(fh)
    return {"pts": pts, "codes": codes, "base": base,
            "meta": {k: {"name": win[k]["name"], "a": win[k]["a"],
                         "b": win[k]["b"], "eq": win[k]["eq"],
                         "only": sum(1 for c in codes[k] if c == -1)}
                     for k in win}}


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #

# Same tokens as webapp/explorer.html, so the two pages read as one product and
# a map lifted from there will need no restyling.
CSS = """
:root { color-scheme: light;
  --bg:#f6f9fb; --panel:#fff; --panel-2:#eef1f6; --ink:#1d1d24; --ink-2:#445a80;
  --ink-3:#7b88a1; --rule:#dbe1ec; --rule-2:#c3ccdc; --accent:#008b7b;
  --brand:#243754; --brand-ink:#fff; --good:#1b7f3b; --bad:#c62828;
  --shadow:0 1px 2px rgba(36,55,84,.06), 0 8px 24px rgba(36,55,84,.08); }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  color-scheme: dark; --bg:#1b1b23; --panel:#262631; --panel-2:#2f2f3c;
  --ink:#eceef4; --ink-2:#b9c2d6; --ink-3:#8b93a8; --rule:#3a3a48;
  --rule-2:#4a4a5c; --good:#4caf6d; --bad:#ef5350; } }
* { box-sizing:border-box }
body { margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.5 "IBM Plex Sans",system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
.mast { background:var(--brand); color:var(--brand-ink); padding:14px 22px; }
.mast h1 { margin:0; font-size:17px; font-weight:600; letter-spacing:.01em }
.mast .sub { font-size:12.5px; opacity:.85; margin-top:3px }
.wrap { max-width:1180px; margin:0 auto; padding:22px }
section { background:var(--panel); border:1px solid var(--rule); border-radius:9px;
  padding:16px 18px; margin-bottom:20px; box-shadow:var(--shadow) }
h2 { font-size:15px; margin:0 0 4px; font-weight:650 }
.lede { color:var(--ink-2); font-size:12.5px; margin:0 0 14px; max-width:78ch }
table { border-collapse:collapse; width:100%; font-size:12.5px }
th,td { padding:5px 9px; border-bottom:1px solid var(--rule); text-align:left;
  vertical-align:baseline }
th { font-size:10.5px; letter-spacing:.06em; text-transform:uppercase;
  color:var(--ink-3); font-weight:600; border-bottom:1px solid var(--rule-2) }
td.num,th.num { text-align:right; font-variant-numeric:tabular-nums }
tbody tr:hover { background:var(--panel-2) }
.grp { font-weight:650; background:var(--panel-2) }
.up { color:var(--good); font-weight:600 } .down { color:var(--bad); font-weight:600 }
.bar { display:inline-block; height:9px; border-radius:2px; vertical-align:middle }
.runA { color:var(--ink-2) } .runB { color:var(--ink) ; font-weight:600 }
.pill { display:inline-block; padding:1px 7px; border-radius:999px; font-size:11px;
  border:1px solid var(--rule-2); margin-right:6px }
.mx { border-collapse:collapse; font-size:12px; margin-top:8px }
.mx td,.mx th { border:1px solid var(--rule); padding:4px 8px; text-align:center }
.mx td.diag { background:var(--panel-2); font-weight:600 }
.note { color:var(--ink-3); font-size:11.5px; margin-top:10px }
#mapwrap { position:relative; background:#e1e8f2; border:1px solid var(--rule);
  border-radius:7px; overflow:hidden }
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]) #mapwrap { background:#22303f } }
#mapsvg { display:block; width:100%; height:auto }
.key { display:flex; flex-wrap:wrap; gap:14px; margin:10px 0 4px; font-size:12px }
.key span.sw { width:11px; height:11px; border-radius:3px; display:inline-block;
  margin-right:6px; vertical-align:-1px; box-shadow:0 0 0 1px rgba(0,0,0,.18) inset }
select { font:inherit; font-size:12.5px; padding:3px 7px; border-radius:6px;
  border:1px solid var(--rule-2); background:var(--panel); color:var(--ink) }
details > summary { cursor:pointer; color:var(--ink-3); font-size:11.5px;
  font-weight:600; letter-spacing:.04em; padding:4px 0 }
"""


def esc(s) -> str:
    return html.escape(str(s))


def fmt(v, dec: int) -> str:
    return "—" if v is None or not np.isfinite(v) else f"{v:.{dec}f}"


def render(a: dict, b: dict, j: pd.DataFrame, rows: list[dict],
           trans: list[dict], top: dict, mp: dict | None = None) -> str:
    la, lb = esc(a["label"]), esc(b["label"])
    win = f"{a['cfg'].get('date_start','?')} .. {a['cfg'].get('date_end','?')}"
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{lb} vs {la} — VPU comparison</title><style>{CSS}</style></head><body>
<div class="mast"><h1>{lb} <span style="opacity:.6">compared with</span> {la}</h1>
<div class="sub">{esc(win)} &middot; {len(j):,} gauges scored by both &middot;
{j.attrs['lost_a']:,} only in {la}, {j.attrs['lost_b']:,} only in {lb}</div></div>
<div class="wrap">"""]

    parts.append(f"""<section><h2>What is being compared</h2>
<p class="lede">Only gauges present in <b>both</b> runs are compared, because two
models built on different river networks do not score the same gauges — and if
the ones a model lacks are systematically the hard ones, comparing the two
populations flatters it for a reason that has nothing to do with the model.
Every figure below is a <b>paired</b> difference: same gauge, same observations,
same days, only the model differs.</p>
<p><span class="pill">{la}</span>{j.attrs['n_a']:,} gauges &rarr;
<b>{j.attrs['n_a']-j.attrs['lost_a']:,}</b> compared</p>
<p><span class="pill">{lb}</span>{j.attrs['n_b']:,} gauges &rarr;
<b>{j.attrs['n_b']-j.attrs['lost_b']:,}</b> compared</p>
<p class="note">Improvement always means <i>closer to the metric's optimum</i>,
never simply larger — PBIAS is better near zero, FAR near zero, alpha, beta and
gamma near one.</p></section>""")

    # ---- grouped summary --------------------------------------------------
    parts.append(f"""<section><h2>By group</h2>
<p class="lede">Median of each run, and the share of gauges where {lb} sits closer
to the optimum than {la}. Groups with fewer than {MIN_GROUP} paired gauges are
omitted. This is the best-supported view here: a grouping splits ~{len(j):,}
gauges into a handful of buckets, so each figure rests on hundreds.</p>""")
    seen = None
    for grouping in dict.fromkeys(r["grouping"] for r in rows):
        parts.append(f"<h3 style='font-size:13px;margin:16px 0 4px'>{esc(grouping)}</h3>"
                     "<table><thead><tr><th>Group</th><th>Metric</th>"
                     f"<th class='num'>{la}</th><th class='num'>{lb}</th>"
                     "<th class='num'>change</th>"
                     "<th class='num'>improved at</th><th></th></tr></thead><tbody>")
        for r in [x for x in rows if x["grouping"] == grouping]:
            if r["group"] != seen:
                parts.append(f"<tr class='grp'><td colspan='7'>{esc(r['group'])} "
                             f"<span style='font-weight:400;color:var(--ink-3)'>"
                             f"n={r['n']:,}</span></td></tr>")
                seen = r["group"]
            gain = abs(r["a"] - r["opt"]) - abs(r["b"] - r["opt"])
            cls = "up" if gain > 0 else ("down" if gain < 0 else "")
            arrow = "&#9650;" if gain > 0 else ("&#9660;" if gain < 0 else "&ndash;")
            sh = r["share_improved"]
            w = max(1, round(sh * 90))
            col = "var(--good)" if sh >= .5 else "var(--bad)"
            parts.append(
                f"<tr><td></td><td>{esc(r['metric'])}</td>"
                f"<td class='num runA'>{fmt(r['a'], r['dec'])}</td>"
                f"<td class='num runB'>{fmt(r['b'], r['dec'])}</td>"
                f"<td class='num {cls}'>{arrow} {fmt(abs(gain), r['dec'])}</td>"
                f"<td class='num'>{100*sh:.0f}%</td>"
                f"<td><span class='bar' style='width:{w}px;background:{col}'></span></td></tr>")
        parts.append("</tbody></table>")
    parts.append("</section>")

    # ---- decision transitions ---------------------------------------------
    if trans:
        parts.append(f"""<section><h2>Decision verdicts</h2>
<p class="lede">A verdict moving Weak &rarr; Good is not a difference of two
numbers, it is a move between categories, so these are counted rather than
averaged. Rows are {la}, columns are {lb}; the shaded diagonal is gauges that did
not move. Anything above the diagonal improved.</p>""")
        for t in trans:
            parts.append(f"<h3 style='font-size:13px;margin:16px 0 2px'>{esc(t['name'])}</h3>"
                         f"<p class='note' style='margin:0 0 6px'>{t['n']:,} gauges &middot; "
                         f"<span class='up'>{t['better']:,} improved</span> &middot; "
                         f"{t['same']:,} unchanged &middot; "
                         f"<span class='down'>{t['worse']:,} worse</span></p>")
            parts.append("<table class='mx'><thead><tr><th></th>"
                         + "".join(f"<th>{esc(x)}</th>" for x in t["labels"])
                         + "</tr></thead><tbody>")
            for i, lab in enumerate(t["labels"]):
                cells = "".join(
                    f"<td class='{'diag' if i == k else ''}'>{v or ''}</td>"
                    for k, v in enumerate(t["matrix"][i]))
                parts.append(f"<tr><th>{esc(lab)}</th>{cells}</tr>")
            parts.append("</tbody></table>")
        parts.append("</section>")

    # ---- map ---------------------------------------------------------------
    if mp and mp["pts"]:
        parts.append(f"""<section><h2>Where each run wins</h2>
<p class="lede">Coloured by <b>which run scored closer to the metric's
optimum</b>, not by how much. The size of a per-gauge difference is poorly
determined; its SIGN is not, because the comparison is paired &mdash; same gauge,
same observations, same days. "About equal" is a band, since a difference of
1e-9 always has a sign and colouring it would be noise.</p>
<div class="key">
  <span><span class="sw" style="background:#2a78d6"></span>{la} better</span>
  <span><span class="sw" style="background:#7b3fa0"></span>{lb} better</span>
  <span><span class="sw" style="background:#9aa5b8"></span>about equal</span>
  <span><span class="sw" style="background:#c9a227"></span>scored by only one run</span>
</div>
<p style="margin:8px 0 10px"><label style="font-weight:650">Colour by
<select id="mapmetric" style="font-weight:600"></select></label>
<span id="mapcount" class="note" style="margin-left:10px"></span></p>
<div id="mapwrap"><svg id="mapsvg" role="img"
  aria-label="gauges coloured by which run performed better"></svg></div>
<p class="note">Gauges in the fourth colour exist in only one of the two runs and
are not compared anywhere else on this page &mdash; they are drawn because a
comparison most easily misleads by quietly omitting them.</p>
<script>
const MP = {json.dumps(mp, separators=(",", ":"))};
// Blue against PURPLE, not against orange or red. The two runs are two things,
// not a good one and a bad one -- an orange/red arm reads as failure whichever
// run it lands on. Purple is far enough from blue in hue to separate cleanly and
// carries no verdict; it is also the one hue explorer.html already established
// as distinguishable from both arms of its diverging ramp under protanopia.
const COL = {{"1":"#2a78d6","2":"#7b3fa0","0":"#9aa5b8","-1":"#c9a227"}};
const svg = document.getElementById("mapsvg");
const sel = document.getElementById("mapmetric");
Object.keys(MP.meta).forEach(k => {{
  const o = document.createElement("option");
  o.value = k; o.textContent = MP.meta[k].name; sel.appendChild(o);
}});
const lons = MP.pts.map(p => p[0]), lats = MP.pts.map(p => p[1]);
const x0 = Math.min(...lons), x1 = Math.max(...lons);
const y0 = Math.min(...lats), y1 = Math.max(...lats);
const padx = (x1-x0)*0.04 || 1, pady = (y1-y0)*0.04 || 1;
const LX0 = x0-padx, LX1 = x1+padx, LY0 = y0-pady, LY1 = y1+pady;
const KX = Math.cos((LY0+LY1)/2 * Math.PI/180);
const W = 1100, H = Math.max(320, Math.round(W * (LY1-LY0) / ((LX1-LX0)*KX)));
const PX = v => (v - LX0) / (LX1 - LX0) * W;
const PY = v => H - (v - LY0) / (LY1 - LY0) * H;
function draw() {{
  const k = sel.value, code = MP.codes[k];
  let s = `<rect width="${{W}}" height="${{H}}" fill="none"/>`;
  for (const layer of ["world","countries","states","coast","lakes"]) {{
    const paths = MP.base[layer] || [];
    const stroke = layer === "lakes" ? "#c6d4e6" : "#b9c6d8";
    for (const line of paths) {{
      let d = "";
      for (let i=0;i<line.length;i++) d += (i?"L":"M") + PX(line[i][0]).toFixed(1)
        + " " + PY(line[i][1]).toFixed(1);
      s += `<path d="${{d}}" fill="none" stroke="${{stroke}}" stroke-width="0.7"/>`;
    }}
  }}
  // Draw "about equal" and "only one run" first so a win is never hidden under them.
  for (const want of [0,-1,1,2]) {{
    for (let i=0;i<MP.pts.length;i++) {{
      if (code[i] !== want) continue;
      const p = MP.pts[i];
      s += `<circle cx="${{PX(p[0]).toFixed(1)}}" cy="${{PY(p[1]).toFixed(1)}}" r="2.6"`
         + ` fill="${{COL[String(want)]}}" fill-opacity="0.85"`
         + ` stroke="rgba(0,0,0,.25)" stroke-width="0.4"><title>${{p[2]}}</title></circle>`;
    }}
  }}
  svg.setAttribute("viewBox", `0 0 ${{W}} ${{H}}`);
  svg.innerHTML = s;
  const m = MP.meta[k];
  document.getElementById("mapcount").textContent =
    `${{m.a.toLocaleString()}} ${{JSON.parse(document.getElementById("labels").textContent)[0]}} · `
    + `${{m.b.toLocaleString()}} ${{JSON.parse(document.getElementById("labels").textContent)[1]}} · `
    + `${{m.eq.toLocaleString()}} about equal · ${{m.only.toLocaleString()}} in only one run`;
}}
sel.onchange = draw; draw();
</script>
<script id="labels" type="application/json">{json.dumps([a["label"] + " better", b["label"] + " better"])}</script>
</section>""")

    # ---- movers ------------------------------------------------------------
    if top:
        parts.append(f"""<section><h2>Gauges that moved most</h2>
<p class="lede">By KGE'. These are the tails, shown so a large shift has somewhere
to be seen — they are <b>not</b> evidence that these particular rivers changed,
since the extremes of any distribution are where sampling noise lands hardest.
A map of per-gauge change is deliberately not here yet; whether it would show
signal or scatter depends on how large the real differences turn out to be.</p>
<table><thead><tr><th>Gauge</th><th class='num'>{la}</th><th class='num'>{lb}</th>
<th class='num'>change</th><th>Gauge</th><th class='num'>{la}</th>
<th class='num'>{lb}</th><th class='num'>change</th></tr></thead><tbody>""")
        for g, w in zip(top.get("better", []), top.get("worse", [])):
            parts.append(
                f"<tr><td>{esc(g['g'])}</td><td class='num'>{g['a']:.2f}</td>"
                f"<td class='num'>{g['b']:.2f}</td>"
                f"<td class='num up'>&#9650; {g['gain']:.2f}</td>"
                f"<td>{esc(w['g'])}</td><td class='num'>{w['a']:.2f}</td>"
                f"<td class='num'>{w['b']:.2f}</td>"
                f"<td class='num down'>&#9660; {abs(w['gain']):.2f}</td></tr>")
        parts.append("</tbody></table></section>")

    parts.append("</div></body></html>")
    return "".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", required=True, help="output directory of the FIRST run")
    ap.add_argument("--b", required=True, help="output directory of the SECOND run")
    ap.add_argument("--vpu", type=int, default=714)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    a, b = load_run(args.a, args.vpu), load_run(args.b, args.vpu)
    print(f"[1/4] {a['label']}  vs  {b['label']}")
    check_windows(a, b)
    print("[2/4] pairing gauges")
    j = intersect(a, b)

    groups = [("overall", "Overall"), ("strmOrder", "Stream order")]
    for g in a["cfg"].get("groups", []):
        groups.append((g["key"], g["name"]))
    print(f"[3/4] comparing {len(METRIC_SPECS)} metrics over "
          f"{len(groups)} groupings")
    rows = metric_rows(j, groups)
    trans = transition_rows(j)
    top = movers(j, "kge_2012", 1.0)
    win = winners(j)
    win.update(decision_winners(j))
    mp = map_payload(a, b, j, win, args.vpu)

    out = args.out or os.path.join(
        os.path.dirname(args.b.rstrip("/")) or ".",
        f"vpu{args.vpu}_compare.html")
    print(f"[4/4] writing {out}")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render(a, b, j, rows, trans, top, mp))
    print(f"\nwrote {out}")
    if trans:
        for t in trans:
            print(f"  {t['name']:20s} {t['better']:5,} better  "
                  f"{t['same']:5,} same  {t['worse']:5,} worse")


if __name__ == "__main__":
    main()
