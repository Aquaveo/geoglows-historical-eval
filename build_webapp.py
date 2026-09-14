#!/usr/bin/env python
"""Build the self-contained gauge-explorer page for one VPU.

Takes the metrics parquet written by kge_map.py, adds a per-gauge flow-duration
curve and monthly regime (observed and simulated, both on the paired sample),
embeds simplified basemap outlines, and injects the lot into webapp/explorer.html.

Everything is inlined into a single HTML file -- no tile server, no CDN, no
backend. That constrains what can be shown per gauge: summary curves fit, full
daily hydrographs (~155 MB for this VPU) do not.

Usage
-----
    python build_webapp.py                 # VPU 714
    python build_webapp.py --vpu 714 --out outputs/vpu714_explorer.html

    --label   Name for the run, shown on the page and in its browser tab.
              Defaults to whatever kge_map.py recorded in vpu<VPU>_run.json, so
              it normally needs no setting; pass it to rename a page without
              recomputing the metrics.
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np
import pandas as pd

from kge_map import (CACHE_DIR, DATA_DIR, DATE_END, DATE_START, KGE_NO_SKILL,
                     RUN_LABEL, data_paths, framing_bbox, load_gauge_series)

# Exceedance probabilities (%) for the flow-duration curve. Denser in the tails,
# because that is where the interesting model failures live.
FDC_EXCEED = [0.1, 0.5, 1, 2, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 98, 99, 99.9]

# Boundary simplification tolerance in degrees. 0.02 keeps state shapes readable
# at the zoom levels this page supports while staying small enough to inline.
SIMPLIFY_TOL = 0.02

# Token in webapp/explorer.html that receives the payload. Must appear once.
PLACEHOLDER = "__" + "DATA" + "__"


def r4(x):
    """Round for JSON: 4 significant digits, None for non-finite."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    if f == 0:
        return 0.0
    return float(f"{f:.4g}")


def load_model_cache(vpu: int, start: str, end: str) -> pd.DataFrame:
    path = os.path.join(CACHE_DIR, f"model_q_vpu{vpu}_{start}_{end}.npz")
    if not os.path.exists(path):
        raise SystemExit(f"missing {path} -- run kge_map.py first to populate the cache")
    z = np.load(path, allow_pickle=False)
    return pd.DataFrame(
        z["q"],
        index=pd.DatetimeIndex(z["dates"].astype("datetime64[ns]"), name="date"),
        columns=z["river_ids"].astype("int64"),
    )


def gauge_curves(metrics: pd.DataFrame, model: pd.DataFrame,
                 gauge_dir: str) -> dict:
    """Flow-duration curve and monthly regime per gauge, on the paired sample."""
    print(f"[2/4] computing curves for {len(metrics)} gauges")
    q_levels = [1.0 - p / 100.0 for p in FDC_EXCEED]  # exceedance -> quantile
    curves = {}

    for i, row in enumerate(metrics.itertuples(index=False), start=1):
        if i % 500 == 0:
            print(f"      {i}/{len(metrics)}")
        obs = load_gauge_series(os.path.join(gauge_dir, row.fname))
        if obs is None or row.final_river_id not in model.columns:
            continue
        both = pd.concat([model[row.final_river_id].rename("sim"),
                          obs.rename("obs")], axis=1, join="inner").dropna()
        if both.empty:
            continue

        o, s = both["obs"], both["sim"]
        mon = both.groupby(both.index.month).mean()
        mon = mon.reindex(range(1, 13))

        curves[str(row.final_river_id)] = {
            "fo": [r4(v) for v in o.quantile(q_levels).to_numpy()],
            "fs": [r4(v) for v in s.quantile(q_levels).to_numpy()],
            "mo": [r4(v) for v in mon["obs"].to_numpy()],
            "ms": [r4(v) for v in mon["sim"].to_numpy()],
        }
    return curves


def basemap_paths(bbox: tuple[float, float, float, float]) -> dict:
    """Simplified state and coastline polylines clipped to the bounding box.

    Read straight from the Natural Earth shapefiles cartopy already cached, so
    this adds no new download.
    """
    print("[3/4] building basemap outlines")
    import cartopy.io.shapereader as shpreader
    from shapely.geometry import box
    from shapely.ops import unary_union

    lon0, lat0, lon1, lat1 = bbox
    clip = box(lon0 - 2, lat0 - 2, lon1 + 2, lat1 + 2)
    # "countries" is the one that tells you where on Earth you are. It was
    # missing: admin_1 gives SUB-national boundaries only, so a VPU outside the
    # handful of countries Natural Earth details at that level got nothing --
    # VPU 208 rendered 0 admin_1 polylines and was left with bare coastline.
    layers = {
        "countries": ("cultural", "admin_0_boundary_lines_land"),
        "states": ("cultural", "admin_1_states_provinces_lakes"),
        "coast": ("physical", "coastline"),
        "lakes": ("physical", "lakes"),
    }

    out = {}
    for name, (cat, ne_name) in layers.items():
        try:
            path = shpreader.natural_earth(resolution="50m", category=cat, name=ne_name)
            geoms = [g for g in shpreader.Reader(path).geometries() if g.intersects(clip)]
        except Exception as exc:                       # pragma: no cover
            print(f"      skipped {name}: {exc}")
            out[name] = []
            continue

        lines = []
        for g in geoms:
            g = g.intersection(clip).simplify(SIMPLIFY_TOL, preserve_topology=True)
            if g.is_empty:
                continue
            for part in (g.geoms if hasattr(g, "geoms") else [g]):
                coords = (list(part.exterior.coords)
                          if part.geom_type == "Polygon" else list(part.coords))
                if len(coords) > 1:
                    lines.append([[r4(x), r4(y)] for x, y in coords])
        out[name] = lines
        print(f"      {name}: {len(lines)} polylines")
    return out


METRIC_FIELDS = [
    "kge_2012", "nse", "r", "alpha", "beta", "gamma",
    "spearman",
    "pbias_pct", "rmse", "mae", "nrmse", "mae_rel", "ss_clim", "ss_clim_mae",
    "mean_obs", "mean_sim", "sd_obs", "sd_sim", "n_pairs", "n_clim",
] + ['n_years_ams', 't2_obs', 't2_sim', 'hits', 'false_alarms', 'misses', 'correct_neg', 'pod', 'far', 'csi', 'ets', 'freq_bias']
# Every metric also exists per calendar month as <metric>_m<MM>. Sent in full so
# the by-month table and the summary tab can show any of them.
MONTHLY_PREFIXES = ['n', 'mean_obs', 'mean_sim', 'sd_obs', 'sd_sim', 'r', 'spearman', 'alpha', 'beta', 'gamma', 'pbias_pct', 'kge_2012', 'nse', 'rmse', 'mae', 'nrmse', 'mae_rel', 'n_clim', 'ss_clim', 'ss_clim_mae']
# Monthly metrics ship as one 12-element array per metric, not as 240 separate
# keys. Key names were 70% of that block's bytes in the built page.


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vpu", type=int, default=714)
    # Default to the window kge_map.py actually ran with, not to the module
    # constants. These select the model cache file AND label the window in the
    # page, so a mismatch either fails to find the cache or mislabels the page.
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--metrics", default=None)
    ap.add_argument("--template", default="webapp/explorer.html")
    ap.add_argument("--out", default=None)
    # Defaults to the label kge_map.py recorded, so the page cannot end up
    # naming a different run than the metrics came from. Passing it renames the
    # page without recomputing anything.
    ap.add_argument("--label", default=None)
    ap.add_argument("--data-dir", default=DATA_DIR,
                    help="directory holding master_catalog_with_metadata.xlsx "
                         "and routing/gauge_data/. Defaults to $GEOGLOWS_EVAL_DATA.")
    args = ap.parse_args()

    # Same reason as serve.py: the curves are built from the gauge CSVs, and a
    # wrong data dir would quietly produce a page with no observed curves at all.
    # Take the resolved gauge_dir rather than the module-level GAUGE_DIR, which
    # was fixed from the environment at import time and cannot see --data-dir.
    _, gauge_dir = data_paths(args.data_dir)

    metrics_path = args.metrics or f"outputs/vpu{args.vpu}_metrics.parquet"
    out_path = args.out or f"outputs/vpu{args.vpu}_explorer.html"

    cfg_path = os.path.join(os.path.dirname(metrics_path) or ".",
                            f"vpu{args.vpu}_run.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    else:
        from kge_map import model_period
        _first, _last = model_period()
        print(f"      WARNING: {cfg_path} not found; falling back to the model's "
              f"full record {_first}..{_last}. Re-run kge_map.py.")
        cfg = {"date_start": _first, "date_end": _last}
    if args.start is None:
        args.start = cfg["date_start"]
    if args.end is None:
        args.end = cfg["date_end"]
    # run.json written before --label existed has no "label" key.
    if args.label is None:
        args.label = cfg.get("label") or RUN_LABEL
    print(f"      metric window {args.start} .. {args.end}")
    print(f"      run label     {args.label}")

    print(f"[1/4] loading {metrics_path}")
    m = pd.read_parquet(metrics_path)
    print(f"      {len(m)} gauges")

    # The cache is named after the window kge_map.py FETCHED, which is earlier
    # than the evaluation start whenever --warmup-years was used. Load by
    # cache_start, then trim to args.start so the curves here are computed on
    # exactly the days the metrics were. Older run.json files have no
    # cache_start and never had a warm-up trim, so the two coincide.
    model = load_model_cache(args.vpu, cfg.get("cache_start", args.start), args.end)
    before = len(model)
    model = model.loc[model.index >= pd.Timestamp(args.start)]
    if len(model) != before:
        print(f"      warm-up trim: {before - len(model)} days dropped, "
              f"{len(model)} scored from {args.start}")
    curves = gauge_curves(m, model, gauge_dir)

    *bbox, n_outside = framing_bbox(m.longitude, m.latitude)
    bbox = tuple(bbox)
    if n_outside:
        print(f"      {n_outside} gauges fall outside the framed area and are "
              f"drawn but do not set the view")
    base = basemap_paths(bbox)

    gauges = []
    for row in m.itertuples(index=False):
        rec = {
            "id": int(row.final_river_id),
            "g": str(row.gauge_id),
            "lon": r4(row.longitude),
            "lat": r4(row.latitude),
            "so": int(row.strmOrder) if pd.notna(row.strmOrder) else None,
            # USContArea is m^2; convert to km^2 for display.
            "da": r4(row.USContArea / 1e6) if pd.notna(row.USContArea) else None,
            "kp": str(row.koppen) if pd.notna(row.koppen) else None,
            "cc": str(row.ISO_A3),
            "rn": (str(row.river_name) if getattr(row, "river_name", None)
                   and str(row.river_name) != "nan" else None),
            "y0": pd.Timestamp(row.first_day).year,
            "y1": pd.Timestamp(row.last_day).year,
        }
        for f in METRIC_FIELDS:
            rec[f] = r4(getattr(row, f, None))
        for pre in MONTHLY_PREFIXES:
            rec[pre + "_m"] = [r4(getattr(row, f"{pre}_m{mm:02d}", None))
                               for mm in range(1, 13)]
        gauges.append(rec)

    payload = {
        "vpu": args.vpu,
        "label": args.label,
        "window": [args.start, args.end],
        "noSkill": KGE_NO_SKILL,
        "fdcExceed": FDC_EXCEED,
        "bbox": [r4(v) for v in bbox],
        "nOutside": n_outside,
        "gauges": gauges,
        "curves": curves,
        "basemap": base,
    }

    blob = json.dumps(payload, separators=(",", ":"))
    print(f"[4/4] payload {len(blob)/1e6:.1f} MB")

    with open(args.template, encoding="utf-8") as fh:
        html = fh.read()
    # Exactly one placeholder, replaced exactly once. Guarding this is not
    # paranoia: writing the token in a code comment once caused the entire
    # payload to be injected twice, doubling the file, and JSON containing the
    # sequence "*/" would have closed that comment and broken the page.
    n_slots = html.count(PLACEHOLDER)
    if n_slots != 1:
        raise SystemExit(f"{args.template}: expected exactly 1 {PLACEHOLDER} "
                         f"placeholder, found {n_slots}")
    html = html.replace(PLACEHOLDER, blob, 1)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"      wrote {out_path} ({os.path.getsize(out_path)/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
