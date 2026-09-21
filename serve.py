#!/usr/bin/env python
"""Local server for the gauge explorer, with full daily series per river.

Unlike the self-contained page built by build_webapp.py, this version embeds no
time series. When you click a gauge it slices that reach out of the model array
in memory, reads the observed CSV off disk, and returns both plus the observed
day-of-year climatology -- so the page can draw a daily hydrograph, which is far
too large to inline for every gauge.

The model array is the cache .npz kge_map.py wrote, i.e. exactly the numbers the
metrics were computed from, whatever their source. Everything is clipped to the
evaluation window, so a warm-up year excluded from the metrics is drawn nowhere.

That is why this one needs a server: a published artifact is a static page in a
browser sandbox and cannot run Python. Run this locally instead.

    python serve.py                 # then open http://localhost:8765
    python serve.py --port 9000 --vpu 714
    python serve.py --metrics outputs_v7/vpu714_metrics.parquet --port 8766

Serving two runs on two ports is how they get compared side by side; --label
names each one on its page. Uses only the standard library plus pandas/numpy --
no Flask.

The modelled side is served entirely from the cached local array. The observed
side is read per click, from the gauge bucket by default or from a local copy
with --data-dir, so a click on a new gauge costs one read (~0.2s against S3).
Gauges are cached in-process after first read, which is what makes going back
and forth between two of them feel instant.
"""
from __future__ import annotations

import argparse
import json
import os
import traceback
from functools import lru_cache
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

from kge_map import (CACHE_DIR, KGE_NO_SKILL, add_gauge_source_args,
                     framing_bbox, load_gauge_series, note_unused_data_dir,
                     source_from_args)

HERE = os.path.dirname(os.path.abspath(__file__))
# One source file for both deployments. Served with the __DATA__ placeholder
# intact, which is how the page knows to run live against /api.
APP_HTML = os.path.join(HERE, "webapp", "explorer.html")
# v2 in the name on purpose: the layer set changed when the whole-globe `world`
# outline was added, and a cache written before that is missing it silently --
# the map would simply refuse to show anything when zoomed out. Bump this
# whenever basemap_paths() gains or drops a layer.
BASEMAP_CACHE = os.path.join(HERE, "cache", "basemap_v2_vpu{vpu}.json")

# Half-width of the centred, circular smoothing window applied to the observed
# day-of-year climatology. Raw DOY means are noisy at a single gauge; ~15 days
# either side gives a readable seasonal reference without flattening the peak.
CLIM_SMOOTH = 15

# Flood-detection block: whole record only, no monthly counterpart.
CONTINGENCY_FIELDS = ('n_years_ams', 't2_obs', 't2_sim', 'hits', 'false_alarms', 'misses', 'correct_neg', 'pod', 'far', 'csi', 'ets', 'freq_bias')

# Metrics that kge_map.py also computes per calendar month.
MONTHLY_PREFIXES = ['n', 'mean_obs', 'mean_sim', 'sd_obs', 'sd_sim', 'r', 'spearman', 'alpha', 'beta', 'gamma', 'pbias_pct', 'kge_2012', 'nse', 'rmse', 'mae', 'nrmse', 'mae_rel', 'n_clim', 'ss_clim', 'ss_clim_mae']

# Decision mode, written by `kge_map.py --mode decision` to a separate parquet.
# Merged onto the metric rows by reach id when that file is present, so the page
# offers decisions and statistics in one picker. A gauge scored in the metric
# table but absent from the decision table keeps null here and draws as no
# value -- which is the honest rendering: not "no skill", but not enough record
# to place the category breakpoints at all.
# hs_verdict is the traffic light itself; the rest are what it was computed
# from, kept so a gauge's panel can say WHY it is the colour it is. The page
# colours by hs_verdict alone -- decision mode offers no metric choice.
DECISION_FIELDS = ('hs_verdict', 'hs_xdry_pod', 'hs_xdry_n_events',
                   'hs_xdry_p_luck', 'hs_xdry_hits', 'hs_xdry_misses',
                   'hs_n_months', 'hs_min_n_month',
                   'tr_verdict', 'tr_obs', 'tr_sim', 'tr_n_years',
                   'fl_verdict', 'fl_csi', 'fl_n_obs', 'fl_n_sim', 'fl_hits',
                   'fl_false', 'fl_p_luck',
                   'wd_verdict')

# Every verdict column, each filled with its own grey code for gauges the
# decision run could not score. A gauge may answer one decision and not the
# other, so they are filled independently.
VERDICT_COLS = ('hs_verdict', 'tr_verdict', 'fl_verdict')

STATE: dict = {}


def r3(x):
    if x is None:
        return None
    f = float(x)
    if not np.isfinite(f):
        return None
    return float(f"{f:.4g}")


# --------------------------------------------------------------------------- #
# Startup data
# --------------------------------------------------------------------------- #

def load_run_config(vpu: int, metrics_path: str) -> dict:
    """Read the config kge_map.py recorded beside the parquet.

    Falls back to the module constants only if the file is missing, and says so
    loudly: the window is used both to LABEL the metrics and to CLIP the series
    the per-gauge charts are computed over, so guessing it wrong silently
    reports statistics for a period the metrics were not computed on.
    """
    from kge_map import RUN_LABEL
    path = os.path.join(os.path.dirname(metrics_path), f"vpu{vpu}_run.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        # run.json written before --label existed has no "label" key.
        cfg.setdefault("label", RUN_LABEL)
        return cfg
    from kge_map import model_period
    first, last = model_period()
    print(f"  WARNING: {path} not found; falling back to the model's full record "
          f"{first}..{last}. Re-run kge_map.py to regenerate it.")
    return {"vpu": vpu, "label": RUN_LABEL,
            "date_start": first, "date_end": last}


def decisions_beside(metrics_path: str, vpu: int) -> str | None:
    """Path to the decision parquet next to the metric one, if it was written."""
    p = os.path.join(os.path.dirname(metrics_path) or ".",
                     f"vpu{vpu}_decisions.parquet")
    return p if os.path.exists(p) else None


def merge_decisions(m: pd.DataFrame, path: str, cfg: dict) -> pd.DataFrame:
    """Join the decision columns onto the metric rows by reach id.

    Refuses to merge two runs scored over different windows. The page reports
    ONE window in its header and on every chart; silently combining a decision
    run over 1980-2020 with a metric run over the full record would label the
    decision columns with a period they were not computed on.
    """
    d = pd.read_parquet(path)
    dcfg_path = os.path.join(os.path.dirname(path) or ".",
                             os.path.basename(path).replace(".parquet",
                                                            "_run.json"))
    if os.path.exists(dcfg_path):
        with open(dcfg_path, encoding="utf-8") as fh:
            dcfg = json.load(fh)
        same = (dcfg.get("date_start") == cfg.get("date_start")
                and dcfg.get("date_end") == cfg.get("date_end"))
        if not same:
            # Carried into the payload as well as printed. Refusing the merge
            # silently looks identical to "this feature was never built" from
            # the browser, and the server log is not where anyone is looking.
            STATE["decision_note"] = (
                f"Decision scores were not loaded: they cover "
                f"{dcfg.get('date_start')}..{dcfg.get('date_end')} but the "
                f"metrics cover {cfg.get('date_start')}..{cfg.get('date_end')}. "
                f"Re-run both over the same window.")
            print("\n  " + "!" * 68)
            print(f"  {STATE['decision_note']}")
            print(f"  python kge_map.py --vpu {cfg.get('vpu')} --mode decision "
                  f"--start {cfg.get('date_start')} --end {cfg.get('date_end')}")
            print("  " + "!" * 68 + "\n")
            return m
    keep = ["final_river_id"] + [c for c in DECISION_FIELDS if c in d.columns]
    merged = m.merge(d[keep], on="final_river_id", how="left")
    # A gauge in the metric table but absent from the decision table could not
    # be scored at all. It becomes grey rather than null, so it still draws --
    # "we could not judge this" is a result, and on VPU 122 it is 96% of them.
    from kge_map import VERDICT_GREY, TREND_GREY, FLOOD_GREY
    greys = {"hs_verdict": VERDICT_GREY, "tr_verdict": TREND_GREY,
             "fl_verdict": FLOOD_GREY}
    counts = []
    for col in VERDICT_COLS:
        if col not in merged:
            continue
        n = int(merged[col].notna().sum())
        merged[col] = merged[col].fillna(greys[col]).astype(int)
        counts.append(f"{col.split('_')[0]} {n}")
    # Derived here, not in the parquet: it is banded straight off the `spearman`
    # column the metric table already carries, so the bands can be retuned
    # without re-running kge_map.py.
    if "spearman" in merged:
        from kge_map import wetdry_verdict
        merged["wd_verdict"] = merged["spearman"].map(wetdry_verdict).astype(int)
        counts.append(f"wd {int((merged['wd_verdict'] >= 0).sum())}")
    print(f"  decisions: {', '.join(counts)} scored of {len(merged)} gauges "
          f"({os.path.basename(path)})")
    return merged


def load_gauges(vpu: int, metrics_path: str) -> dict:
    m = pd.read_parquet(metrics_path)
    dpath = decisions_beside(metrics_path, vpu)
    if dpath:
        m = merge_decisions(m, dpath, STATE["cfg"])
    _bb = framing_bbox(m.longitude, m.latitude)
    rows = []
    for r in m.itertuples(index=False):
        rec = {
            "id": int(r.final_river_id), "g": str(r.gauge_id),
            "lon": r3(r.longitude), "lat": r3(r.latitude),
            "so": int(r.strmOrder) if pd.notna(r.strmOrder) else None,
            "da": r3(r.USContArea / 1e6) if pd.notna(r.USContArea) else None,
            "kp": str(r.koppen) if pd.notna(r.koppen) else None,
            "cc": str(r.ISO_A3),
            "rn": (str(r.river_name) if getattr(r, "river_name", None)
                   and str(r.river_name) != "nan" else None),
            "fname": str(r.fname) if hasattr(r, "fname") else None,
            "y0": pd.Timestamp(r.first_day).year, "y1": pd.Timestamp(r.last_day).year,
        }
        fields = ("kge_2012", "nse", "r", "alpha", "beta", "gamma", "pbias_pct",
                  "rmse", "mae", "nrmse", "mae_rel", "ss_clim", "ss_clim_mae",
                  "spearman",
                  "mean_obs", "mean_sim", "sd_obs", "sd_sim", "n_pairs",
                  "n_clim") + CONTINGENCY_FIELDS + DECISION_FIELDS
        # Monthly metrics ship as one 12-element array per metric rather than
        # 240 separate keys. The key names were 70% of that block's bytes, so
        # this removes ~7 MB from the static build without dropping a single
        # value or reimplementing any formula in JavaScript.
        for f in fields:
            rec[f] = r3(getattr(r, f, None))
        for pre in MONTHLY_PREFIXES:
            rec[pre + "_m"] = [r3(getattr(r, f"{pre}_m{mm:02d}", None))
                               for mm in range(1, 13)]
        rows.append(rec)
    return {"gauges": rows,
            "bbox": [r3(v) for v in _bb[:4]],
            "nOutside": _bb[4],
            "vpu": vpu, "noSkill": KGE_NO_SKILL,
            "label": STATE["cfg"]["label"],
            # Why decision mode is absent, when a decision parquet exists but
            # could not be used. None when there is nothing to explain.
            "dNote": STATE.get("decision_note"),
            "window": [STATE["cfg"]["date_start"], STATE["cfg"]["date_end"]]}


def load_basemap(vpu: int, bbox) -> dict:
    path = BASEMAP_CACHE.format(vpu=vpu)
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    from build_webapp import basemap_paths
    bm = basemap_paths(tuple(bbox))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(bm, fh)
    return bm


# --------------------------------------------------------------------------- #
# Per-river series
# --------------------------------------------------------------------------- #

def load_model_array(cfg: dict) -> pd.DataFrame:
    """The exact model array the metrics were computed from, clipped to the window.

    This reads the cache .npz that kge_map.py wrote, NOT the GEOGLOWS store. It
    used to call geoglows.data.retro_daily() per reach, which was harmless only
    while the scored model and the published retrospective were the same numbers.
    They are not once a run scores routed output or any other source: the page
    would draw a GEOGLOWS hydrograph beside metrics computed from something else,
    with nothing on screen saying so.

    Rows before the evaluation start are dropped here, so a warm-up year that was
    excluded from the metrics is also absent from every chart. Charts and numbers
    then describe the same days, and the window no longer has to be shaded.

    cache_start names the file (it is the window that was FETCHED); date_start is
    where scoring began. They differ only when --warmup-years was used.
    """
    lo = cfg.get("cache_start") or cfg["date_start"]
    path = os.path.join(CACHE_DIR, f"model_q_vpu{cfg['vpu']}_{lo}_{cfg['date_end']}.npz")
    if not os.path.exists(path):
        raise SystemExit(
            f"missing {path}\n"
            f"  serve.py plots the same model array the metrics came from, so that\n"
            f"  file has to exist. Re-run kge_map.py for this window to rebuild it.")
    z = np.load(path, allow_pickle=False)
    df = pd.DataFrame(
        z["q"],
        index=pd.DatetimeIndex(z["dates"].astype("datetime64[ns]"), name="date"),
        columns=z["river_ids"].astype("int64"),
    )
    n_before = len(df)
    df = df.loc[df.index >= pd.Timestamp(cfg["date_start"])]
    trimmed = n_before - len(df)
    print(f"  model array {os.path.basename(path)}: {len(df):,} days x "
          f"{df.shape[1]:,} reaches"
          + (f"  ({trimmed:,} warm-up days dropped)" if trimmed else ""))
    return df


def model_series(river_id: int) -> pd.Series:
    """Daily mean modelled discharge for one reach, from the loaded array."""
    q = STATE["model"]
    if river_id not in q.columns:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], name="date"))
    return q[river_id].dropna()


def climatology(obs: pd.Series) -> list:
    """Smoothed observed day-of-year mean, 366 values, index 0 = Jan 1."""
    doy = obs.groupby(obs.index.dayofyear).mean().reindex(range(1, 367))
    v = doy.to_numpy(dtype="float64")
    # Circular smoothing so 31 Dec and 1 Jan are neighbours.
    pad = np.concatenate([v[-CLIM_SMOOTH:], v, v[:CLIM_SMOOTH]])
    out = np.full(366, np.nan)
    for i in range(366):
        w = pad[i:i + 2 * CLIM_SMOOTH + 1]
        w = w[np.isfinite(w)]
        if w.size:
            out[i] = w.mean()
    return [r3(x) for x in out]


@lru_cache(maxsize=512)
def cached_gauge_series(fname: str):
    """One gauge's observed series, remembered across clicks.

    Unlike the batch scripts this reads one gauge per request, so against S3
    every click is a fresh ~0.2s round trip -- and revisiting a gauge, which is
    exactly what comparing two of them involves, paid it again. The cache is
    per-process and the CSVs are immutable within a snapshot, so there is
    nothing to invalidate; 512 gauges is a few tens of MB at most.
    """
    return load_gauge_series(STATE["source"].locate(fname), STATE["source"])


def build_series(river_id: int, fname: str | None) -> dict:
    sim = model_series(river_id)
    obs = cached_gauge_series(fname) if fname else None

    # The axis is the EVALUATION window and nothing outside it. The model array
    # was already clipped to it at startup; the gauge is clipped to match here,
    # so a warm-up year excluded from the metrics is drawn nowhere either, and
    # observations from years the model does not cover are not plotted against
    # empty space.
    cfg = STATE.get("cfg", {})
    w_lo = pd.Timestamp(cfg["date_start"])
    w_hi = pd.Timestamp(cfg["date_end"])
    idx = pd.date_range(w_lo, w_hi, freq="D")

    sim_a = sim.reindex(idx)
    obs_a = obs.reindex(idx) if obs is not None else pd.Series(np.nan, index=idx)

    # Every returned day is inside the window now, so the charts and the metrics
    # describe the same sample by construction. `win` is kept because the
    # frontend indexes with it; it now spans the whole series.
    w0, w1 = 0, len(idx)

    return {
        "id": river_id,
        "t0": idx[0].strftime("%Y-%m-%d"),
        "n": len(idx),
        "doy0": int(idx[0].dayofyear),
        # half-open [w0, w1) index range of the metric window within sim/obs
        "win": [w0, w1],
        "window": [cfg["date_start"], cfg["date_end"]],
        "sim": [r3(x) if np.isfinite(x) else None for x in sim_a.to_numpy()],
        "obs": [r3(x) if np.isfinite(x) else None for x in obs_a.to_numpy()],
        "clim": climatology(obs_a.dropna()) if obs_a.notna().any() else None,
    }


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            print(f"  {self.path.split('?')[0]}  {args[1] if len(args) > 1 else ''}")

    def _json(self, obj, code=200):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)

        if u.path in ("/", "/index.html"):
            try:
                with open(APP_HTML, "rb") as fh:
                    body = fh.read()
            except FileNotFoundError:
                self.send_error(404, f"missing {APP_HTML}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if u.path == "/api/gauges":
            self._json(STATE["payload"])
            return

        if u.path == "/api/series":
            q = parse_qs(u.query)
            try:
                rid = int(q["id"][0])
            except (KeyError, ValueError, IndexError):
                self._json({"error": "pass ?id=<river_id>"}, 400)
                return
            fname = STATE["fname_by_id"].get(rid)
            try:
                self._json(build_series(rid, fname))
            except Exception as exc:
                traceback.print_exc()
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        self.send_error(404)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vpu", type=int, default=714)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--metrics", default=None)
    # Defaults to the label kge_map.py recorded beside the parquet. Pass it to
    # override without recomputing; serving two runs on two ports is the reason
    # the page needs a name at all.
    ap.add_argument("--label", default=None)
    add_gauge_source_args(ap)
    args = ap.parse_args()

    # The gauge CSVs are read per click. Resolve the source up front: without
    # this a wrong data directory or a missing credential yields a server that
    # starts fine and then draws every observed series as empty, which looks
    # like missing gauge data rather than a misconfiguration.
    STATE["source"] = source_from_args(args)
    note_unused_data_dir(args, STATE["source"])
    print(f"gauges: {STATE['source'].describe()}")

    metrics = args.metrics or os.path.join(HERE, "outputs",
                                           f"vpu{args.vpu}_metrics.parquet")
    print(f"loading {metrics}")
    STATE["cfg"] = load_run_config(args.vpu, metrics)
    if args.label:
        STATE["cfg"]["label"] = args.label
    print(f"  metric window {STATE['cfg']['date_start']} .. {STATE['cfg']['date_end']}")
    print(f"  run label     {STATE['cfg']['label']}")
    # Loaded once and held in memory: the whole array is ~100-350 MB float32 and
    # every gauge click is then a column slice rather than a file read.
    STATE["model"] = load_model_array(STATE["cfg"])
    payload = load_gauges(args.vpu, metrics)
    print(f"  {len(payload['gauges'])} gauges")
    payload["basemap"] = load_basemap(args.vpu, payload["bbox"])

    STATE["payload"] = payload
    STATE["fname_by_id"] = {g["id"]: g.pop("fname") for g in payload["gauges"]}

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    # Say where the observations come from, because it decides whether a click
    # is instant or a round trip -- and whether the server works offline at all.
    obs = ("read per click from " + STATE["source"].describe()
           if STATE["source"].kind == "s3" else
           "on local disk at " + STATE["source"].describe())
    print(f"\n  Gauge Skill Explorer -> http://localhost:{args.port}\n"
          f"  run: {STATE['cfg']['label']}\n"
          f"  modelled series: local model array, no network\n"
          f"  observed series: {obs}\n"
          f"  Ctrl-C to stop\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
