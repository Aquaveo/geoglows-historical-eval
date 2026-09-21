#!/usr/bin/env python
"""Score modelled daily discharge against observed gauges, for one VPU.

WHAT THIS SCRIPT IS
-------------------
This reads modelled daily mean discharge, pairs it against local observed gauge
CSVs, and writes the files listed under OUTPUTS WRITTEN below. These outputs can 
be read by serve.py and build_webapp.py to produce a web page with a map of the results.
This is designed to be a pipeline for evaluating a model's output. You may run this several times
with different model outputs, and the outputs will be written to different directories with 
different labels. The web page will then show the results for each model output.

The model discharge comes from the GEOGLOWS retrospective daily zarr on S3 by
default, or from a parquet file given with --model-parquet. Sub-daily input is
averaged to daily mean. Either way the array is cached locally and reused, so
only the first run pays for reading it.


USAGE
-----
    python kge_map.py --vpu <VPU>                 whole model record
    python kge_map.py --vpu <VPU> --start 1990-01-01 --end 2020-12-31
    python kge_map.py --vpu <VPU> --min-years 5
    python kge_map.py --vpu <VPU> --refresh       re-read the zarr, ignore the cache
    python kge_map.py --vpu <VPU> --label "routing v7" --outdir outputs_v7
    python kge_map.py --vpu <VPU> --warmup-years 1
    python kge_map.py --vpu <VPU> --model-parquet routed.parquet --label "routing v7"

    --vpu         VPU code to evaluate. Defaults to 714
    --model-parquet
                  Score a parquet of modelled discharge instead of the
                  retrospective. Layout: a datetime index and one column per
                  river, named with its reach id. Sub-daily input is averaged to
                  DAILY MEAN, matching how the daily zarr is built. 
    --start/--end Restrict the window FETCHED. Default is the
                  whole record.This window names the cache file, and with --warmup-years it
                  is not the window actually scored -- see that flag.
    --min-years   Minimum years of paired overlap a gauge must have. Default 1.
                  under MIN_YEARS_FOR_RETURN they get no return level and no
                  flood-detection scores, and under MIN_DAYS_PER_MONTH in a
                  given month they get no metrics for that month.
    --warmup-years  Drop this many years from the START of the fetched record
                  before scoring, so spin-up from an assumed initial state is
                  not evaluated. The trim happens AFTER the fetch, so the cache
                  still covers the untrimmed window and is not invalidated.
    --refresh     Discard the cached model array and re-read it.
    --outdir      Where to write outputs. Default outputs/.
    --data-dir    Directory holding local gauge inputs -- routing/gauge_data/
                  and, optionally, the catalog xlsx. Defaults to
                  $GEOGLOWS_EVAL_DATA. Only needed when reading gauges locally,
                  or to supply what the bucket catalog lacks (see below).
    --gauge-source  local or s3. Default LOCAL, whenever
                  <data-dir>/routing/gauge_data/ exists. S3 is opt-in: pass s3
                  here or an --aws-profile. Resolved before anything else, so a
                  wrong path or a missing credential fails immediately.
    --aws-profile Named AWS profile for the gauge bucket. Omit to use the
                  standard credential chain -- default profile, environment
                  variables, instance role -- which is what lets this run
                  unchanged in CI or on an EC2 box.
    --gauge-date-tag
                  Which published gauge snapshot to read. Recorded in run.json,
                  because it is part of every key and a new publication moves
                  every score.
    --label       Name for this run, shown on the map footer, the web page and
                  its browser tab. Default RUN_LABEL below.

INPUTS REQUIRED
  Gauge CSVs        Columns datetime,discharge. READ FROM A LOCAL DIRECTORY BY
                    DEFAULT -- <data-dir>/routing/gauge_data/, named
                    {ISO_A3}_{provider}_{station}.csv. That is the preferred
                    source: faster, no credentials, works offline. A catalogued
                    gauge with no CSV is skipped and reported in the run log.

                    The same gauges are also published to the private bucket
                    s3://master-gauge-data, which is OPT-IN: pass --gauge-source
                    s3 or an --aws-profile. With neither, and no local
                    directory, the run stops rather than reaching for a bucket
                    most people cannot access.
  Gauge catalog     <data-dir>/master_catalog_with_metadata.xlsx for a local
                    run. An S3 run needs no local input -- the bucket publishes
                    its own catalog, one catalog.csv per provider -- but that
                    one carries no Koppen group, so an S3 run loses that
                    grouping. The bucket is authoritative for reach matching
                    either way; see KNOWN_ISSUES section S for why the xlsx's
                    extra 840 matches are duplicates rather than a gap.
  Network table     Fetched from S3 at run time. The v2-model-table Supplies stream order and
                    drainage area.
  Model discharge   Normally the daily zarr, fetched from S3 at run time --
                    nothing to prepare. To score a different model instead, pass
                    --model-parquet and point it at a parquet file; nothing else
                    needs preparing for that either.

                    Those two are the only inputs. Either way the array read is
                    cached to cache/model_q_vpu<VPU>_<start>_<end>.npz and reused
                    on the next run -- that .npz is an internal cache, not an
                    input format to prepare yourself. It records which source
                    built it, so pointing --model-parquet at a different file
                    rebuilds rather than silently reusing the previous one, and a
                    cache carrying no such stamp is rejected and rebuilt.
                    --refresh forces a rebuild when the source has not changed.

OUTPUTS WRITTEN
  outputs/vpu<VPU>_metrics.parquet   one row per gauge, every metric below
  outputs/vpu<VPU>_kge_map.png       static map of KGE' at gauge locations
  outputs/vpu<VPU>_run.json          the settings this run used, read back by
                                     serve.py and build_webapp.py: vpu, label,
                                     date_start (the EVALUATION start, after any
                                     warm-up trim), date_end, cache_start (the
                                     window fetched, which names the cache file),
                                     warmup_years, min_years, n_gauges, and the
                                     KGE column name, label and no-skill line.
  cache/model_q_vpu<VPU>_<start>_<end>.npz    the model array, GAUGED REACHES
                                     ONLY, reused by later runs and read back by
                                     serve.py and build_webapp.py



NOTATION
--------
The model array is restricted on BOTH axes before anything is computed.

  Rivers   Only reaches that carry a gauge are ever read. VPU 714 has 223,906
           reaches and about 2,600 gauges, so this is most of what makes a run
           affordable -- and it means the cached array is NOT a copy of the
           model for that VPU, only the gauged slice of it.
  Days     Only the requested window, minus any --warmup-years trim.

Everything is computed per gauge, over PAIRED DAYS ONLY -- days where that gauge
and the model both have a value. The paired record therefore differs per gauge.

    o, s      observed / simulated daily mean discharge, m3 s-1
    n         number of paired days
    mo, ms    mean of o / of s
    so, ss    standard deviation of o / of s   (population, ddof=0)
    r         Pearson correlation of s against o

ddof=0 means the standard deviations divide by n, not by n-1 -- the population
convention rather than the sample one.


THE METRICS, IN THE ORDER THEY ARE COMPUTED
-------------------------------------------
Sufficient statistics -- pair_stats(). Six numbers from which the whole
NSE/KGE/RMSE family can be re-derived later without touching the zarr again:

    n_pairs                n
    mean_obs, mean_sim     mo, ms
    sd_obs, sd_sim         so, ss
    r                      Pearson correlation

  column        formula                              range      best
  ------------  -----------------------------------  ---------  ----
  r             Pearson(s, o)                        [-1, 1]     1
  spearman      Pearson(rank s, rank o)              [-1, 1]     1
  alpha         ss / so            variability ratio [0, inf)    1
  beta          ms / mo            bias ratio        (-inf, inf) 1
  gamma         (ss/ms) / (so/mo)  CV ratio          [0, inf)    1
  pbias_pct     100 * (beta - 1)                     (-inf, inf) 0
  kge_2012      1 - sqrt((r-1)^2 + (gamma-1)^2 + (beta-1)^2)
                                                     (-inf, 1]   1
  rmse          sqrt(mean((s-o)^2))                  [0, inf)    0   m3/s
  nse           1 - (rmse/so)^2                      (-inf, 1]   1
  mae           mean(|s-o|)                          [0, inf)    0   m3/s
  nrmse         rmse / mo                            [0, inf)    0
  mae_rel       mae / mo                             [0, inf)    0

Skill against the gauge's own day-of-year climatology -- skill_scores():

  n_clim        paired days with a usable climatology reference
  ss_clim       1 - MSE(model) / MSE(climatology)    (-inf, 1]   1
  ss_clim_mae   1 - MAE(model) / MAE(climatology)    (-inf, 1]   1

Flood detection at each series' own 2-year return level -- contingency_stats().
Whole record only; the threshold is an annual property so there is no monthly
counterpart. Each series is compared to ITS OWN 2-year level, so a model that
runs low is not penalised for never reaching the gauge's absolute flow:

  n_years_ams   annual maxima available (>= 10 required)
  t2_obs        2-year return level of the observed record, m3/s
  t2_sim        2-year return level of the modelled record, m3/s
  hits          a   days both series are at or above their own level
  false_alarms  b   model at or above its level, gauge not
  misses        c   gauge at or above its level, model not
  correct_neg   d   neither
                    a + b + c + d = n, the paired days

  pod           a/(a+c)               probability of detection: the share of the
                                      gauge's flood days the model also flagged
  far           b/(a+b)               false alarm ratio: the share of the model's
                                      flood days the gauge did not flag
  csi           a/(a+b+c)             critical success index: hits over every day
                                      either series flagged, ignoring d
  ets           (a-ar)/(a+b+c-ar)     equitable threat score: csi with the hits
                                      expected by chance removed,
                                      ar = (a+b)(a+c)/n
  freq_bias     (a+b)/(a+c)           days the model flagged over days the gauge
                                      flagged; 1 means the same number, not the
                                      same days

The 2-year level comes from a Gumbel Type-I fit to the annual maxima series, 
with a minimum of 10 years.

Per calendar month, MM = 01..12 -- monthly_stats(). Pools the same month across
every year, so "08" is every August day in the record. EVERY metric above has a
monthly counterpart, named <metric>_m<MM>:

  n_m<MM>                  paired days in that month
  mean_obs_m<MM>           mo within that month
  mean_sim_m<MM>           ms
  sd_obs_m<MM>             so
  sd_sim_m<MM>             ss
  r_m<MM>                  Pearson
  spearman_m<MM>           Spearman rank correlation
  alpha_m<MM>              ss / so
  beta_m<MM>               ms / mo
  gamma_m<MM>              (ss/ms) / (so/mo)
  pbias_pct_m<MM>          100 * (beta - 1)
  kge_2012_m<MM>           KGE' from that month's own r, gamma, beta
  nse_m<MM>                1 - (rmse/so)^2
  rmse_m<MM>               m3/s
  mae_m<MM>                m3/s
  nrmse_m<MM>              rmse / mo
  mae_rel_m<MM>            mae / mo
  n_clim_m<MM>             days in that month with a climatology reference
  ss_clim_m<MM>            MSE skill vs climatology, scored on that month
  ss_clim_mae_m<MM>        MAE skill vs climatology, scored on that month

That is 20 metrics x 12 months = 240 columns. The parquet is wider still once
the whole-record metrics, the flood-detection block and the gauge metadata are
added; read its shape rather than trusting a number written here.

FOUR THINGS ABOUT THE MONTHLY VALUES
  1. Each one is calculated fresh from that month's days alone. Nothing is
     carried over from the annual figures, so the twelve monthly values do not
     average back to the annual one and cannot be added or decomposed into it.
     Compare them with each other, not with the annual number.
  2. ss_clim_m<MM> keeps the SAME leave-one-year-out day-of-year reference the
     annual score uses -- the reference is not rebuilt per month -- and scores it
     over that month's days only.
  3. A month with fewer than MIN_DAYS_PER_MONTH (60) paired days gets only
     n_m<MM>; everything else is absent for that month.
  4. The guards are per-metric. rmse_m<MM> and mae_m<MM> only average the errors
     themselves, so they are always defined; every other metric divides by a
     statistic of the data and is absent when that divisor is zero -- r, alpha
     and nse need a non-flat observed series, and beta, nrmse and mae_rel need a
     non-zero observed mean. A month can therefore have rmse and mae while
     lacking the rest, rather than being dropped entirely.

Gauge metadata carried through for grouping and mapping: final_river_id,
gauge_id, fname, latitude, longitude, strmOrder, USContArea (m2), koppen,
ISO_A3, first_day, last_day.

"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
from scipy.stats import rankdata

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Where the observed-gauge inputs live. None of this is guessed: the two things
# a local run needs are named outright, by --gauge-dir and --catalog or their
# environment variables. There is no useful default -- the gauge CSVs are not
# part of this repository.
#
#     GEOGLOWS_EVAL_GAUGE_DIR   directory of {ISO}_{provider}_{station}.csv
#     GEOGLOWS_EVAL_CATALOG     the gauge catalog, .xlsx or .csv
#
# GEOGLOWS_EVAL_DATA stays as a shorthand for the layout download_observed_data.py
# happens to produce -- <dir>/routing/gauge_data/ beside
# <dir>/master_catalog_with_metadata.xlsx -- but it is only a way of filling in
# the two above, never an assumption imposed on anyone who lays their files out
# differently. Anything given explicitly wins over it.
DATA_DIR = os.environ.get("GEOGLOWS_EVAL_DATA", "data")
GAUGE_DIR_ENV = os.environ.get("GEOGLOWS_EVAL_GAUGE_DIR")
CATALOG_ENV = os.environ.get("GEOGLOWS_EVAL_CATALOG")

# Only used to fill in what --data-dir implies, never to search for files.
CONVENTION_GAUGES = os.path.join("routing", "gauge_data")
CONVENTION_CATALOG = "master_catalog_with_metadata.xlsx"


def local_paths(data_dir: str | None = None, gauge_dir: str | None = None,
                catalog: str | None = None) -> tuple[str | None, str | None]:
    """Work out where the gauge CSVs and the catalog are. Explicit wins.

    Returns (gauge_dir, catalog), either of which may be None when nothing said
    where it is -- deciding whether that is fatal belongs to the caller, because
    an S3 run needs neither.
    """
    gd = gauge_dir or GAUGE_DIR_ENV
    cat = catalog or CATALOG_ENV
    if (gd is None or cat is None) and data_dir:
        if gd is None:
            gd = os.path.join(data_dir, CONVENTION_GAUGES)
        if cat is None:
            cat = os.path.join(data_dir, CONVENTION_CATALOG)
    return gd, cat


def data_paths(data_dir: str) -> tuple[str, str]:
    """Back-compat shim: the conventional layout under `data_dir`, validated.

    Kept because it is the one path-resolving helper other scripts imported
    before --gauge-dir and --catalog existed. New code should call local_paths().
    """
    catalog = os.path.join(data_dir, CONVENTION_CATALOG)
    gauges = os.path.join(data_dir, CONVENTION_GAUGES)
    missing = [p for p in (catalog, gauges) if not os.path.exists(p)]
    if missing:
        raise SystemExit(
            "cannot find the observed-gauge inputs:\n"
            + "".join(f"  missing: {p}\n" for p in missing)
            + f"  looked under: {data_dir}\n"
            "  set GEOGLOWS_EVAL_DATA or pass --data-dir to the directory holding\n"
            "  master_catalog_with_metadata.xlsx and routing/gauge_data/")
    return catalog, gauges


# Resolved in main() from --data-dir; module-level defaults let serve.py and
# build_webapp.py import GAUGE_DIR without re-parsing arguments.
CATALOG_PATH = os.path.join(DATA_DIR, "master_catalog_with_metadata.xlsx")
GAUGE_DIR = os.path.join(DATA_DIR, "routing", "gauge_data")

# The private bucket the gauges are published to. Reading needs s3:ListBucket on
# the bucket and s3:GetObject on production/*; credentials come from boto's own
# chain, never from this repository. See README, "What you need before running".
GAUGE_BUCKET = "master-gauge-data"

# The snapshot the gauges are read from. It is part of every key, so a new
# publication changes every score -- which is why it is written into run.json
# rather than left implicit. Every prefix in the bucket carries this one tag
# today; when that stops being true this becomes a real choice.
GAUGE_DATE_TAG = "20251008"

# Parallel gauge reads. Each is a separate round trip of ~0.2s, so a VPU's worth
# of them is entirely latency: 2,600 gauges take ~8 minutes serially and under a
# minute at this width. Matches the width download_observed_data.py uses.
GAUGE_WORKERS = 12

ZARR_URL = "http://geoglows-v2.s3-us-west-2.amazonaws.com/retrospective/daily.zarr"
MODEL_TABLE_URL = "http://geoglows-v2.s3-us-west-2.amazonaws.com/tables/v2-model-table.parquet"

# The zarr time axis is "seconds since 1940-01-01", daily steps.
ZARR_EPOCH = pd.Timestamp("1940-01-01")

# Fetch window for the model array -- and the evaluation window too, unless
# --warmup-years trims the front of it. None means "everything the model covers",
# resolved from the zarr's own time axis at run time so it does not go stale as
# the retrospective is extended.
#
# Both windows reach outputs/vpu<VPU>_run.json, which serve.py and
# build_webapp.py read back: the fetch window as cache_start, the evaluation
# window as date_start. They differ only when a warm-up trim is used.
#
# Freeze the window explicitly when comparing model versions, or a longer
# retrospective will change the baseline underneath the comparison.
DATE_START = None
DATE_END = None

# Name for the discharge being scored, carried into vpu<VPU>_run.json and from
# there onto the web page and its browser tab. 
#
# Override it with --label whenever the model array is not the default
# retrospective. 
RUN_LABEL = "GEOGLOWS v2 retrospective"

CACHE_DIR = "cache"
OUTPUT_DIR = "outputs"

ZARR_READ_THREADS = 16
CHUNK_WIDTH = 50  # river_id chunk width of the Q array;

# Columns read at once from a --model-parquet. A wide parquet of routed output
# is gigabytes taken whole; 400 columns keeps peak memory near 1 GB.
PARQUET_COL_BATCH = 400


def source_tag(model_parquet: str | None) -> str:
    """Identity of the discharge source, stamped into the cache .npz.

    The cache is keyed on vpu and window only, which cannot distinguish two
    different sources covering the same dates. 

    Path plus size plus mtime, rather than a content hash: hashing a 2 GB parquet
    on every run costs more than the read it protects.
    """
    if not model_parquet:
        return f"zarr:{ZARR_URL}"
    p = os.path.abspath(model_parquet)
    st = os.stat(p)
    return f"parquet:{p}:{st.st_size}:{int(st.st_mtime)}"


def cache_is_current(path: str, want: str) -> bool:
    """True if the cache at `path` is verifiably built from `want`.

    A cache with no `source` key cannot be verified, so it is rejected and
    rebuilt. Accepting it instead would mean trusting an array whose origin is
    unknown while the run labels its outputs with whatever --label says.
    """
    z = np.load(path, allow_pickle=False)
    if "source" not in z.files:
        print(f"      cache {os.path.basename(path)} carries no source stamp; "
              f"cannot verify what built it, re-reading.")
        return False
    got = str(z["source"])
    if got == want:
        return True
    print(f"      cache {os.path.basename(path)} was built from a different source;"
          f"\n        cached: {got}\n        wanted: {want}\n      re-reading.")
    return False


# The metric is KGE' (Kling et al. 2012) throughout. 
#
# The no-skill line sits at -0.41, not 0 (Knoben et al. 2019). Anything below it is worse than
# predicting the observed mean.
KGE_NO_SKILL = -0.41
KGE_COL = "kge_2012"          # explicit in the column name on purpose
KGE_LABEL = "KGE' (Kling 2012)"

SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
HAIRLINE = "#e1e0d9"

# Diverging red <-> gray <-> blue: red = poor, blue = good, neutral gray at the
# centre. Both arms are monotonic in lightness and mirror each other within
# 0.024 relative luminance, so neither side reads as "heavier" than the other.
#
# The palest step of each arm is deliberately omitted. With them included the
# two steps flanking the midpoint measure dE 14.1 in normal vision -- below the
# 15 floor -- so gauges just above and just below the centre were not reliably
# distinguishable. Dropping them puts the flanking pair at dE 15.0 (protan) /
# 21.6 (normal vision), which clears both floors.
DIVERGING = [
    "#661a1a", "#8f2424", "#b52f2f", "#d03b3b", "#dd625b", "#ea8a84",  # poor
    "#f0efec",                                                          # neutral
    "#6da7ec", "#3987e5", "#2a78d6", "#256abf", "#184f95", "#0d366b",  # good
]

# The colour scale is centred on 0 and clipped to +/- 1. Zero is the
# conventional reading line, but it is NOT the statistical no-skill point --
# that is KGE_NO_SKILL, which is marked separately on the colourbar.
COLOR_LIMIT = 1.0

LAND = "#f4f3ef"
WATER = "#eaeae6"


# --------------------------------------------------------------------------- #
# Gauge table
# --------------------------------------------------------------------------- #

def provider_for(row: pd.Series) -> str:
    """Reproduce the provider-name normalization used by download_observed_data.py.

    The gauge CSV filenames are {ISO_A3}_{provider}_{gauge_id}.csv, so this has to
    match that script exactly or the files will not be found.
    """
    p = row.get("excel_Name of Providing Entity")
    if p is None or (isinstance(p, float) and math.isnan(p)) or str(p).strip().lower() == "nan":
        p = row.get("jorge_Data_Source")
    p = str(p).strip()
    if p.lower() == "nan" or p == "":
        p = "unknown"
    if p == "Togo":
        p = "DGRE"
    if "India-WRIS" in p:
        p = "WRIS"
    return p


def gauge_filename(row: pd.Series) -> str:
    station = str(row.get("gauge_id")).strip()
    if station.endswith(".0"):
        station = station[:-2]
    return f"{str(row['ISO_A3']).strip()}_{provider_for(row)}_{station}.csv"


def _station_id(v) -> str:
    """gauge_id as it appears in a filename or an S3 key.

    pandas reads all-numeric gauge ids as floats, so an id like 12345678 arrives
    as '12345678.0' and would match nothing. Alphanumeric ids -- Canadian HYDAT
    station codes, for instance -- are unaffected, which is why this cannot just
    be an int cast. Both backends key on this, so they have to strip it the same
    way.
    """
    s = str(v).strip()
    return s[:-2] if s.endswith(".0") else s


# --------------------------------------------------------------------------- #
# Gauge source -- where the catalog and the observed CSVs are read from
# --------------------------------------------------------------------------- #
#
# Two backends, one shape. Both answer the same two questions and nothing else
# downstream knows which one it is talking to:
#
#     catalog()      every catalogued gauge, as a DataFrame
#     resolve(g)     given the gauges for one VPU, attach a `loc` column saying
#                    where each one's CSV is, and DROP the ones that have none
#     open(loc)      that CSV, as a file object
#
# Splitting resolve() from catalog() is what keeps the S3 backend affordable.
# The catalog is global (~37,500 gauges) but the measurement listings are
# per-provider, so resolve() lists only the handful of prefixes the VPU actually
# touches -- about seven for VPU 714 -- rather than all 154.

class LocalGaugeSource:
    """Gauges from a directory of CSVs, with a catalog file beside them.

    Both are given, not inferred: `gauge_dir` is wherever the
    {ISO}_{provider}_{station}.csv files actually are, and `catalog_path` is
    whatever the catalog is actually called. Neither has to sit in the layout
    download_observed_data.py produces -- that layout is only what --data-dir
    expands to when someone uses the shorthand.
    """

    kind = "local"

    def __init__(self, gauge_dir: str, catalog_path: str):
        self.gauge_dir = gauge_dir
        self.catalog_path = catalog_path
        missing = [p for p in (gauge_dir, catalog_path) if not os.path.exists(p)]
        if missing:
            raise SystemExit(
                "cannot find the local gauge inputs:\n"
                + "".join(f"  missing: {p}\n" for p in missing)
                + "  --gauge-dir is the directory of per-gauge CSVs;\n"
                "  --catalog is the catalog file itself, .xlsx or .csv.\n"
                "  Checked here rather than at read time: otherwise a wrong path\n"
                "  surfaces as every gauge being skipped, which reads as missing\n"
                "  data rather than a wrong path.")

    def describe(self) -> str:
        return f"local:{os.path.abspath(self.gauge_dir)}"

    def catalog(self) -> pd.DataFrame:
        """The catalog, read by extension rather than assumed to be Excel.

        .csv is accepted because the bucket publishes its catalogs that way, so
        a copy pulled down from there works as a local catalog with no
        conversion. Anything else is tried as Excel, which covers .xls and the
        occasional extensionless file.
        """
        if self.catalog_path.lower().endswith(".csv"):
            return pd.read_csv(self.catalog_path)
        return pd.read_excel(self.catalog_path)

    def resolve(self, g: pd.DataFrame) -> pd.DataFrame:
        g = g.copy()
        g["fname"] = g.apply(gauge_filename, axis=1)
        # One listdir and a set test, not one stat per gauge. Same answer, and
        # it puts the local backend on the same footing as the S3 one.
        try:
            present = set(os.listdir(self.gauge_dir))
        except OSError as e:
            raise SystemExit(f"cannot list {self.gauge_dir}: {e}")
        g["loc"] = g["fname"].map(self.locate)
        return g[g["fname"].isin(present)]

    def locate(self, fname: str) -> str | None:
        return os.path.join(self.gauge_dir, fname)

    def open(self, loc: str):
        return open(loc, "rb")


# production-{ISO_A3}-{provider}-{YYYYMMDD}. Both middle fields are matched
# non-greedily from the ends rather than assumed to be a three-letter code and a
# single word: production-MEKONG-Servir Mekong-20251008 is neither, and a
# stricter pattern silently drops it and every gauge it holds.
_S3_PREFIX_RE = re.compile(r"^production-(.+)-(.+)-(\d{8})$")


class S3GaugeSource:
    """Gauges read directly from the private bucket, nothing downloaded.

    The catalog is assembled from the per-provider catalog.csv files rather than
    from master_catalog_with_metadata.xlsx, so no local input is needed at all.
    Verified equivalent for scoring: across all 154 prefixes it carries 37,529
    gauges to the xlsx's 37,528, final_river_id agrees on every gauge the two
    share, and every one of the 2,626 gauges the VPU 714 baseline scores is
    present.

    The bucket is authoritative for reach matching, INCLUDING WHERE IT DECLINES
    TO MATCH. master_catalog_with_metadata.xlsx carries 840 matches the bucket
    marks -1, and taking them was tried and reverted: they are CARAVAN
    republications of stations the bucket already matches under their native
    provider. See KNOWN_ISSUES section S.

    Nothing local is read here at all. The only column the bucket lacks is
    Koppen group, so an S3 run has `koppen` empty and the web page loses that
    one grouping.
    """

    kind = "s3"

    def __init__(self, profile: str | None = None,
                 date_tag: str = GAUGE_DATE_TAG):
        try:
            import s3fs
        except ImportError:
            raise SystemExit(
                "reading gauges from S3 needs s3fs, which is not installed:\n"
                "  conda env update -f environment.yml\n"
                "  (or pass --data-dir to read a local copy instead)")
        self.date_tag = date_tag
        self.profile = profile
        # No profile pinned unless one was asked for: passing profile= makes boto
        # consult the credentials file ONLY, which breaks env vars, instance
        # roles and SSO. Left alone it runs the whole chain.
        self.fs = s3fs.S3FileSystem(**({"profile": profile} if profile else {}))
        self._prefixes = self._list_prefixes()

    def describe(self) -> str:
        return f"s3://{GAUGE_BUCKET} @ {self.date_tag}"

    def _list_prefixes(self) -> dict[tuple[str, str], str]:
        """{(ISO_A3, provider): prefix} for this snapshot. Also the credential check.

        This is the first call that touches the bucket, and it runs before the
        model table and the zarr, so a credentials problem costs a second rather
        than surfacing thirty seconds in as a botocore traceback -- or, worse,
        as zero gauges found, which reads as an empty basin.
        """
        try:
            names = self.fs.ls(f"{GAUGE_BUCKET}/production/")
        except Exception as e:                                  # noqa: BLE001
            raise SystemExit(self._access_error(e))

        out, unparsed = {}, []
        for p in names:
            name = p.rstrip("/").rsplit("/", 1)[-1]
            if not name.startswith("production-"):
                continue
            m = _S3_PREFIX_RE.match(name)
            if not m:
                unparsed.append(name)
                continue
            iso, provider, tag = m.groups()
            if tag == self.date_tag:
                out[(iso, provider)] = name
        # Loud, not skipped: an unreadable prefix name means its gauges vanish
        # from the catalog, and silently scoring fewer gauges is the failure this
        # whole module is trying to avoid.
        if unparsed:
            raise SystemExit(
                "cannot parse these gauge prefixes:\n"
                + "".join(f"  {n}\n" for n in unparsed)
                + "  expected production-<ISO_A3>-<provider>-<YYYYMMDD>")
        if not out:
            raise SystemExit(
                f"no gauge prefixes carry the tag {self.date_tag} in "
                f"s3://{GAUGE_BUCKET}/production/\n"
                f"  found {len(names)} prefixes with other tags; pass "
                f"--gauge-date-tag to pick one")
        return out

    @staticmethod
    def _access_error(e: Exception) -> str:
        """Turn a boto exception into something that says what to do about it."""
        name, text = type(e).__name__, str(e)
        head = f"cannot read s3://{GAUGE_BUCKET}/production/\n"
        if "NoCredentials" in name or "Unable to locate credentials" in text:
            return head + (
                "  no AWS credentials found.\n"
                "  Set one of: a [default] profile in ~/.aws/credentials,\n"
                "  AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in the environment,\n"
                "  or pass --aws-profile <name>.\n"
                "  To read a local copy instead, pass --data-dir.")
        # Worth separating: a rejected key is a typo or a stale copy, while
        # AccessDenied means the identity is real and simply is not allowed
        # here. They call for different next steps and different people.
        if "InvalidAccessKeyId" in text or "does not exist in our records" in text:
            return head + (
                f"  {text.strip()}\n"
                "  The key itself was rejected -- check for a typo, or for a\n"
                "  rotated key still sitting in ~/.aws/credentials or the\n"
                "  environment. --aws-profile picks a different one.")
        if "SignatureDoesNotMatch" in text:
            return head + (
                f"  {text.strip()}\n"
                "  The key id is known but the secret does not match it.")
        if "AccessDenied" in text or isinstance(e, PermissionError):
            return head + (
                f"  {text.strip()}\n"
                "  The credentials are valid but do not reach this bucket. You need\n"
                f"  s3:ListBucket on {GAUGE_BUCKET} and s3:GetObject on production/*.\n"
                "  Ask whoever administers the GEOGLOWS account.\n"
                "  To read a local copy instead, pass --data-dir.")
        return head + f"  {name}: {text.strip()}"

    def catalog(self) -> pd.DataFrame:
        """Every gauge in the snapshot, from the per-provider catalog.csv files.

        ISO_A3 and provider come from the prefix name, which is what makes this
        backend independent of provider_for(): there is no free-text entity name
        to normalize, because the bucket has already done it.
        """
        def read_one(item):
            (iso, provider), prefix = item
            with self.fs.open(f"{GAUGE_BUCKET}/production/{prefix}/catalog.csv") as f:
                d = pd.read_csv(f)
            d["ISO_A3"] = iso
            d["provider"] = provider
            d["_prefix"] = prefix
            return d

        with ThreadPoolExecutor(max_workers=GAUGE_WORKERS) as pool:
            frames = list(pool.map(read_one, self._prefixes.items()))
        cat = pd.concat(frames, ignore_index=True)

        # catalog.csv flags which series a gauge actually has. Stage-only gauges
        # are dropped here, before resolve() ever lists them -- the local backend
        # can only discover this by opening the CSV and finding no `discharge`
        # column, which is a read per gauge that this backend never pays.
        if "discharge" in cat.columns:
            has_q = cat["discharge"].fillna(0).astype("float64") > 0
            n_stage = int((~has_q).sum())
            cat = cat[has_q]
            print(f"      {n_stage} stage-only gauges dropped from the catalog")
        return cat

    def resolve(self, g: pd.DataFrame) -> pd.DataFrame:
        """Attach the S3 key for each gauge, dropping those with no object.

        Lists measurements/ once per prefix this VPU touches -- ~1.7s each, seven
        of them for VPU 714 -- rather than probing each gauge. That is both far
        cheaper than a HEAD per gauge and a better answer: it distinguishes "not
        published" from "not downloaded", which a local directory cannot.
        """
        g = g.copy()
        g["station"] = g["gauge_id"].map(_station_id)
        wanted = sorted({(iso, p) for iso, p in zip(g["ISO_A3"], g["provider"])})

        def list_one(key):
            prefix = self._prefixes[key]
            base = f"{GAUGE_BUCKET}/production/{prefix}/measurements/"
            try:
                names = self.fs.ls(base)
            except FileNotFoundError:
                return key, base, set()
            return key, base, {n.rsplit("/", 1)[-1][:-4] for n in names
                               if n.endswith(".csv")}

        with ThreadPoolExecutor(max_workers=GAUGE_WORKERS) as pool:
            listed = {k: (base, have) for k, base, have in pool.map(list_one, wanted)}
        print(f"      listed {len(wanted)} provider prefixes")

        def loc_for(row):
            base, have = listed[(row.ISO_A3, row.provider)]
            return base + row.station + ".csv" if row.station in have else None

        g["loc"] = [loc_for(r) for r in g.itertuples(index=False)]
        # fname is the gauge's identity downstream -- it reaches the metrics
        # parquet and both front ends -- so it keeps the local spelling even
        # here, and a parquet from either backend stays interchangeable.
        g["fname"] = (g["ISO_A3"].astype(str).str.strip() + "_"
                      + g["provider"].astype(str).str.strip() + "_"
                      + g["station"] + ".csv")
        return g[g["loc"].notna()]

    def locate(self, fname: str) -> str | None:
        """The S3 key for a gauge named the way the metrics parquet names it.

        This is resolve() in reverse, for the two front ends: they read the
        parquet, which stores `fname` and no location, so a page can be built
        from either backend regardless of which one scored it.

        Split from the ends, not on every underscore: the ISO code and the
        station id have none, but a provider may ('Servir Mekong' is already a
        two-word name), so the middle is whatever is left between them.
        """
        parts = fname[:-4].split("_") if fname.endswith(".csv") else fname.split("_")
        if len(parts) < 3:
            return None
        iso, station, provider = parts[0], parts[-1], "_".join(parts[1:-1])
        prefix = self._prefixes.get((iso, provider))
        if prefix is None:
            return None
        return f"{GAUGE_BUCKET}/production/{prefix}/measurements/{station}.csv"

    def open(self, loc: str):
        return self.fs.open(loc)


def gauge_source(data_dir: str | None, kind: str | None = None,
                 profile: str | None = None,
                 date_tag: str = GAUGE_DATE_TAG,
                 gauge_dir: str | None = None,
                 catalog: str | None = None):
    """Pick a gauge backend. LOCAL IS THE DEFAULT; S3 is never reached by accident.

    A local copy is preferred: it is faster (14.8 ms a gauge against 20.4 ms at
    12 threads, and 148 ms serially), it needs no credentials, and it leaves the
    private bucket out of the loop entirely -- which matters now that this
    repository is public and most people reading it cannot reach that bucket.

    So both ways to end up on S3 are deliberate: --gauge-source s3, or an
    --aws-profile once local has been ruled out. With neither, and no local
    directory, the run STOPS and says what to do rather than quietly reaching for
    the network -- a silent fallback is how someone hits a private bucket without
    meaning to.

    Nothing is guessed. The gauge directory and the catalog are whatever
    --gauge-dir and --catalog say they are; --data-dir only fills those two in
    when they were not given, using the layout download_observed_data.py happens
    to produce. An S3 run reads none of them.
    """
    d = data_dir if data_dir not in (None, DATA_DIR) else (
        data_dir if "GEOGLOWS_EVAL_DATA" in os.environ else None)
    gd, cat = local_paths(d or DATA_DIR, gauge_dir, catalog)

    if kind == "local":
        return LocalGaugeSource(gd, cat)
    if kind == "s3":
        return S3GaugeSource(profile=profile, date_tag=date_tag)

    if gd and os.path.isdir(gd):
        return LocalGaugeSource(gd, cat)

    # --aws-profile names a profile for THIS bucket and nothing else, so passing
    # it is an explicit request for S3 -- but only once local has been ruled out,
    # so a profile left in the environment cannot override a local copy.
    if profile:
        return S3GaugeSource(profile=profile, date_tag=date_tag)

    # Saying WHICH mistake it was is most of the value: a path was given and is
    # wrong, or nothing was configured at all and "data" is the placeholder
    # resolving against the current directory. The second reads as nonsense
    # without this, and is the common one.
    configured = any([gauge_dir, catalog, GAUGE_DIR_ENV, CATALOG_ENV,
                      "GEOGLOWS_EVAL_DATA" in os.environ,
                      data_dir not in (None, DATA_DIR)])
    looked = f"  looked in\n    {os.path.abspath(gd)}\n" if gd else ""
    why = looked if configured else (
        "  Nothing has been configured: none of --gauge-dir, --catalog or\n"
        f"  --data-dir was given and no GEOGLOWS_EVAL_* variable is set, so this\n"
        f"  fell back to ./{DATA_DIR}/ and\n"
        f"  looked in\n    {os.path.abspath(gd)}\n")

    raise SystemExit(
        "no local gauge data, and nothing asked for S3.\n"
        + why
        + "\n"
        "  Preferred -- name the two local inputs, once, for every future shell:\n"
        "    echo 'export GEOGLOWS_EVAL_GAUGE_DIR=/path/to/gauge/csvs' >> ~/.bashrc\n"
        "    echo 'export GEOGLOWS_EVAL_CATALOG=/path/to/catalog.xlsx'  >> ~/.bashrc\n"
        "    source ~/.bashrc\n"
        "  or pass --gauge-dir and --catalog for this run only. The catalog may be\n"
        "  .xlsx or .csv, and neither path has to sit in any particular layout.\n"
        "\n"
        "  Shorthand, if your files are laid out the way download_observed_data.py\n"
        f"  writes them -- <dir>/{CONVENTION_GAUGES}/ beside <dir>/{CONVENTION_CATALOG}:\n"
        "    --data-dir <dir>   (or $GEOGLOWS_EVAL_DATA)\n"
        "\n"
        "  Or read from the private bucket, which needs credentials that reach it:\n"
        f"    --gauge-source s3   (bucket: {GAUGE_BUCKET})\n")


def add_gauge_source_args(ap: argparse.ArgumentParser) -> None:
    """The gauge-source flags, identical across all three scripts."""
    ap.add_argument("--gauge-dir", default=None,
                    help="directory holding the per-gauge CSVs, named "
                         "{ISO_A3}_{provider}_{station}.csv. Defaults to "
                         "$GEOGLOWS_EVAL_GAUGE_DIR. THE PREFERRED WAY to supply "
                         "gauges.")
    ap.add_argument("--catalog", default=None,
                    help="the gauge catalog file, .xlsx or .csv, carrying "
                         "final_river_id, gauge_id, ISO_A3, latitude, longitude. "
                         "Defaults to $GEOGLOWS_EVAL_CATALOG.")
    ap.add_argument("--data-dir", default=DATA_DIR,
                    help="shorthand for both of the above, if your files sit in "
                         f"the layout <dir>/{CONVENTION_GAUGES}/ beside "
                         f"<dir>/{CONVENTION_CATALOG}. Defaults to "
                         "$GEOGLOWS_EVAL_DATA. Ignored for whichever of "
                         "--gauge-dir / --catalog you give explicitly.")
    ap.add_argument("--gauge-source", choices=("local", "s3"), default=None,
                    help="where to read observed gauges from. Default: local, "
                         "whenever <data-dir>/routing/gauge_data/ exists. S3 is "
                         "opt-in -- pass s3 here, or an --aws-profile.")
    ap.add_argument("--aws-profile", default=None,
                    help="named AWS profile for the gauge bucket. Omit to use "
                         "the standard credential chain (default profile, "
                         "environment variables, instance role).")
    ap.add_argument("--gauge-date-tag", default=GAUGE_DATE_TAG,
                    help=f"gauge snapshot to read from S3. Default {GAUGE_DATE_TAG}.")


def source_from_args(args):
    return gauge_source(args.data_dir, args.gauge_source, args.aws_profile,
                        args.gauge_date_tag, args.gauge_dir, args.catalog)


def note_unused_data_dir(args, source) -> None:
    """Say so when --data-dir was passed but cannot affect this run.

    An S3 source reads nothing local, so --data-dir cannot affect the run while
    still looking like it might. Called from every script, since this is now
    true of all three.
    """
    if source.kind != "s3":
        return
    given = [f"--{n.replace('_','-')}" for n, v in
             (("gauge_dir", args.gauge_dir), ("catalog", args.catalog),
              ("data_dir", args.data_dir if args.data_dir != DATA_DIR else None))
             if v]
    if given:
        print(f"note: {', '.join(given)} {'is' if len(given)==1 else 'are'} not "
              f"used when reading gauges from S3.\n"
              f"      They apply to --gauge-source local.")


def build_gauge_table(vpu: int, source) -> pd.DataFrame:
    """Catalog gauges in `vpu` that have a real reach id, network attrs, and a CSV."""
    print(f"[1/5] building gauge table for VPU {vpu} from {source.describe()}")

    cat = source.catalog()
    n_all = len(cat)
    cat = cat.dropna(subset=["final_river_id"]).copy()
    cat["final_river_id"] = cat["final_river_id"].astype(int)

    # final_river_id <= 0 is an unmatched sentinel, not a reach.
    cat = cat[cat["final_river_id"] > 0]
    print(f"      {n_all} catalog rows -> {len(cat)} with a real reach id")

    model = pd.read_parquet(
        MODEL_TABLE_URL,
        columns=["LINKNO", "VPUCode", "strmOrder", "USContArea"],
    )
    model = model[model["VPUCode"] == vpu]
    print(f"      {len(model)} reaches in VPU {vpu}")

    g = cat.merge(model, left_on="final_river_id", right_on="LINKNO", how="inner")
    print(f"      {len(g)} catalog gauges fall in VPU {vpu}")

    # The backend attaches `loc` -- a path or an S3 key -- and drops gauges it
    # has no CSV for. Which of those two it is, is the only difference between a
    # local run and an S3 one from here on.
    before = len(g)
    g = source.resolve(g)
    print(f"      {len(g)} of {before} have a CSV available")

    # Only columns that compute_metrics() carries into the metric rows. Anything
    # kept here but not listed in that st.update() call is silently dropped
    # before the parquet, so the two lists have to be changed together.
    keep = [
        "final_river_id", "gauge_id", "fname", "loc", "latitude", "longitude",
        "ISO_A3", "river_name", "strmOrder", "USContArea",
        # Local runs only -- the bucket catalog has no Koppen equivalent, so an
        # S3 run carries no such column and compute_metrics() writes None.
        "Koppen Group (as of 2024)",
    ]
    keep = [c for c in keep if c in g.columns]
    g = g[keep].rename(columns={"Koppen Group (as of 2024)": "koppen"})

    # gauge_id mixes numeric USGS ids with alphanumeric ones (Canadian HYDAT
    # codes, for instance), so pandas infers `object`; force str for a clean
    # parquet schema.
    for c in ("gauge_id", "koppen", "river_name", "ISO_A3"):
        if c in g.columns:
            g[c] = g[c].astype(str)
    return g.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Model discharge -- from the cache if present, otherwise read from the zarr
# --------------------------------------------------------------------------- #

def model_period() -> tuple[str, str]:
    """First and last date in the retrospective, from the zarr's time axis.

    Only the time coordinate is read (~250 KB), not the discharge array.
    """
    import fsspec
    import zarr
    z = zarr.open(fsspec.get_mapper(ZARR_URL), mode="r")
    t = ZARR_EPOCH + pd.to_timedelta(z["time"][:], unit="s")
    return t[0].strftime("%Y-%m-%d"), t[-1].strftime("%Y-%m-%d")


def fetch_model_q(
    river_ids: np.ndarray, date_start: str, date_end: str, vpu: int, refresh: bool
) -> pd.DataFrame:
    """Return a (time x river_id) DataFrame of modelled daily mean discharge.

    Only the reaches in `river_ids` are read -- the gauged ones. The zarr holds
    6,838,900 rivers and VPU 714 has about 2,600 gauges, so the returned frame
    and the cache written from it are the gauged slice, not the whole model.

    If a cache .npz for this vpu and window exists, carries a matching source
    stamp, and covers every requested reach, it is returned as-is and the zarr is
    never opened. A cache with no stamp, or one built from a different source, is
    rejected and re-read -- see cache_is_current().

    Reading the zarr: the Q array is chunked whole along time and CHUNK_WIDTH
    wide along river_id, so each chunk holds the *full* time series for
    CHUNK_WIDTH rivers. The efficient access pattern is therefore to group the
    wanted columns by chunk and read one chunk-aligned block at a time.
    """
    import fsspec
    import zarr

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"model_q_vpu{vpu}_{date_start}_{date_end}.npz")
    tag = source_tag(None)
    if os.path.exists(cache) and not refresh and cache_is_current(cache, tag):
        z = np.load(cache, allow_pickle=False)
        cached_ids = set(z["river_ids"].astype("int64").tolist())
        wanted = set(np.unique(np.asarray(river_ids)).astype("int64").tolist())
        missing = wanted - cached_ids
        if missing:
            # The cache key is only vpu + window, so it cannot tell that the
            # requested reach set has grown -- which it does as soon as more
            # gauge CSVs are downloaded. Reusing it anyway would drop those
            # gauges, and compute_metrics would report them as "no model
            # column", which reads as a claim about GEOGLOWS coverage rather
            # than a stale local file.
            #
            # Note this re-reads the ZARR and overwrites `cache` in place, so a
            # cache built from a parquet is replaced with retrospective
            # discharge. That is the right outcome only if the zarr is what the
            # run means to score -- when it is not, pass --model-parquet so the
            # rebuild comes from the same source the cache did.
            print(f"[2/5] cache {cache} is missing {len(missing)} of "
                  f"{len(wanted)} requested reaches; re-reading the zarr")
        else:
            print(f"[2/5] model discharge from cache {cache}")
            return pd.DataFrame(
                z["q"],
                index=pd.DatetimeIndex(z["dates"].astype("datetime64[ns]"), name="date"),
                columns=z["river_ids"].astype("int64"),
            )

    # Several gauges can sit on one reach, so the wanted reach ids are not unique.
    # Fetch each reach once; compute_metrics() looks columns up by reach id.
    river_ids = np.unique(np.asarray(river_ids))

    print(f"[2/5] reading model discharge from {ZARR_URL}")
    t0 = time.time()
    z = zarr.open(fsspec.get_mapper(ZARR_URL), mode="r")

    all_ids = z["river_id"][:]
    pos = pd.Series(np.arange(len(all_ids)), index=all_ids)
    idx = pos.reindex(river_ids)
    if idx.isna().any():
        missing = int(idx.isna().sum())
        print(f"      warning: {missing} reach ids absent from the zarr, dropped")
    ok = idx.notna()
    river_ids = np.asarray(river_ids)[ok.values]
    col_idx = idx[ok].astype(int).values

    times = ZARR_EPOCH + pd.to_timedelta(z["time"][:], unit="s")
    tmask = (times >= pd.Timestamp(date_start)) & (times <= pd.Timestamp(date_end))
    # Stop if the window misses the model record entirely. np.argmax on an
    # all-False mask returns 0 rather than signalling failure, and so does the
    # reversed one, so without this check an empty selection silently becomes
    # the WHOLE record -- while the map footer and the cache filename still name
    # the window that was asked for. Fail loudly rather than clamp: a clamped
    # window would still be mislabelled.
    if not tmask.any():
        raise SystemExit(
            f"requested window {date_start} .. {date_end} does not overlap the "
            f"model record ({times[0].date()} .. {times[-1].date()})")
    t_lo, t_hi = int(np.argmax(tmask)), int(len(tmask) - np.argmax(tmask[::-1]))
    times = times[t_lo:t_hi]
    print(f"      {len(river_ids)} reaches x {len(times)} days "
          f"({times[0].date()} to {times[-1].date()})")

    # Group wanted columns by their chunk so each chunk is fetched exactly once.
    by_chunk: dict[int, list[int]] = {}
    for j, c in enumerate(col_idx):
        by_chunk.setdefault(int(c) // CHUNK_WIDTH, []).append(j)
    print(f"      {len(by_chunk)} chunks to fetch on {ZARR_READ_THREADS} threads")

    out = np.full((len(times), len(col_idx)), np.nan, dtype="float32")
    Q = z["Q"]
    done = [0]

    def read_chunk(item):
        chunk, targets = item
        lo = chunk * CHUNK_WIDTH
        block = Q[t_lo:t_hi, lo:lo + CHUNK_WIDTH]
        for j in targets:
            out[:, j] = block[:, int(col_idx[j]) - lo]
        done[0] += 1
        if done[0] % 200 == 0:
            print(f"      {done[0]}/{len(by_chunk)} chunks ({time.time()-t0:.0f}s)")

    with ThreadPoolExecutor(max_workers=ZARR_READ_THREADS) as pool:
        list(pool.map(read_chunk, by_chunk.items()))

    np.savez_compressed(cache, q=out, source=np.array(tag),
                        dates=np.asarray(times, dtype="datetime64[ns]"),
                        river_ids=river_ids.astype("int64"))
    print(f"      done in {time.time()-t0:.0f}s, cached to {cache}")
    return pd.DataFrame(out, index=pd.DatetimeIndex(times, name="date"),
                        columns=river_ids.astype("int64"))


# --------------------------------------------------------------------------- #
# Observed discharge
# --------------------------------------------------------------------------- #

def parquet_period(path: str) -> tuple[str, str]:
    """First and last date in a model-discharge parquet, from its index alone."""
    import pyarrow.parquet as pq
    idx = pq.ParquetFile(path).schema_arrow.names[0]
    t = pd.to_datetime(pq.read_table(path, columns=[idx]).to_pandas().index)
    return t.min().strftime("%Y-%m-%d"), t.max().strftime("%Y-%m-%d")


def model_q_from_parquet(path: str, river_ids: np.ndarray, date_start: str,
                         date_end: str, vpu: int, refresh: bool) -> pd.DataFrame:
    """Read modelled discharge from a wide parquet and cache it as an npz.

    EXPECTED LAYOUT. A datetime index, and one column per river, named with the
    reach id (LINKNO). Column names may be strings -- parquet always stores them
    that way -- and are converted to int. Values are discharge in m3/s.

    ONLY GAUGED REACHES ARE READ. parquet is columnar, so restricting to the
    reaches that actually have a gauge means the other columns are never touched
    on disk. This mirrors what fetch_model_q() does with the zarr.

    SUB-DAILY INPUT IS AVERAGED TO DAILY MEAN, matching how the GEOGLOWS daily
    zarr is built from hourly routing, so the two are comparable. This is not a
    neutral choice: on a 200-reach sample of an hourly routed file the daily
    MAXIMUM ran 39% above the daily mean at the median reach, so a run scored
    against daily maxima would not be comparable with anything else here. The
    aggregation applied is printed.

    The result is written to the same cache .npz that fetch_model_q() uses, so
    build_webapp.py finds it by the same name and a re-run costs nothing.
    """
    import pyarrow.parquet as pq

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"model_q_vpu{vpu}_{date_start}_{date_end}.npz")
    tag = source_tag(path)
    if os.path.exists(cache) and not refresh and cache_is_current(cache, tag):
        z = np.load(cache, allow_pickle=False)
        print(f"[2/5] model discharge from cache {cache}")
        return pd.DataFrame(
            z["q"],
            index=pd.DatetimeIndex(z["dates"].astype("datetime64[ns]"), name="date"),
            columns=z["river_ids"].astype("int64"),
        )

    print(f"[2/5] reading model discharge from {path}")
    t0 = time.time()
    pf = pq.ParquetFile(path)
    names = pf.schema_arrow.names
    idx_col, present = names[0], names[1:]
    if not present:
        sys.exit(f"{path}: no discharge columns beside the index '{idx_col}'")

    # Keep only columns that carry a gauge. Column names are strings in parquet;
    # anything that is not an integer reach id cannot be matched and is skipped.
    wanted = set(int(v) for v in np.unique(np.asarray(river_ids)))
    cols, seen = [], set()
    for c in present:
        try:
            rid = int(c)
        except ValueError:
            continue
        if rid in wanted and rid not in seen:
            cols.append(c)
            seen.add(rid)
    if not cols:
        sys.exit(f"{path}: none of its {len(present)} columns match a gauged "
                 f"reach in VPU {vpu}. Column names must be reach ids "
                 f"(e.g. {present[0]!r} was found).")
    n_absent = len(wanted) - len(cols)
    print(f"      {len(cols):,} of {len(present):,} columns carry a gauge"
          + (f"; {n_absent:,} gauged reaches absent from the file" if n_absent else ""))

    # Read in column batches: a wide parquet of this shape is gigabytes if taken
    # in one piece, and only the daily means are kept.
    parts, ids, dates = [], [], None
    for i in range(0, len(cols), PARQUET_COL_BATCH):
        batch = cols[i:i + PARQUET_COL_BATCH]
        df = pq.read_table(path, columns=[idx_col] + batch).to_pandas()
        df.index = pd.to_datetime(df.index)
        step = df.index.to_series().diff().median()
        df = df.loc[(df.index >= pd.Timestamp(date_start)) &
                    (df.index <= pd.Timestamp(date_end) + pd.Timedelta(days=1)
                     - pd.Timedelta(seconds=1))]
        if df.empty:
            sys.exit(f"{path}: no rows between {date_start} and {date_end}")
        d = df if step >= pd.Timedelta(days=1) else df.resample("D").mean()
        parts.append(d.to_numpy("float32"))
        ids.extend(batch)
        dates = d.index
        if i == 0:
            print(f"      input step {step}; "
                  f"{'kept as daily' if step >= pd.Timedelta(days=1) else 'averaged to DAILY MEAN'}")

    q = np.concatenate(parts, axis=1)
    river_ids = np.asarray([int(c) for c in ids], dtype="int64")

    print(f"      {q.shape[1]} reaches x {q.shape[0]} days "
          f"({dates[0].date()} to {dates[-1].date()}) in {time.time()-t0:.0f}s")
    np.savez_compressed(cache, q=q,
                        dates=np.asarray(dates, dtype="datetime64[ns]"),
                        river_ids=river_ids, source=np.array(tag))
    print(f"      cached to {cache}")
    return pd.DataFrame(q, index=pd.DatetimeIndex(dates, name="date"),
                        columns=river_ids)


def framing_bbox(lon: pd.Series, lat: pd.Series,
                 pad: float = 0.04) -> tuple[float, float, float, float, int]:
    """Map bounds that frame the bulk of the gauges, plus the count left outside.

    NOT min/max. A handful of gauges with corrupt coordinates otherwise decide the
    view for every other gauge: VPU 209 carries 8 French gauges (of 1,852) placed
    in the South Atlantic and off Newfoundland, and they inflate the mapped area
    **23x**, squeezing the real basin into a corner.

    Uses the 0.5th-99.5th percentile of each axis, padded, so up to about 1% of
    gauges can fall outside. They are still drawn and still clickable -- they are
    simply not allowed to set the frame. The count is returned so the page can say
    so rather than hiding it.
    """
    x0, x1 = float(lon.quantile(0.005)), float(lon.quantile(0.995))
    y0, y1 = float(lat.quantile(0.005)), float(lat.quantile(0.995))
    mx, my = (x1 - x0) * pad or 0.1, (y1 - y0) * pad or 0.1
    x0, x1, y0, y1 = x0 - mx, x1 + mx, y0 - my, y1 + my
    outside = int(((lon < x0) | (lon > x1) | (lat < y0) | (lat > y1)).sum())
    return x0, y0, x1, y1, outside


def load_gauge_series(loc: str, source=None) -> pd.Series | None:
    """Read one gauge CSV as a daily discharge series, or None if unusable.

    Returns None for stage-only files: a handful of catalog entries point at CSVs
    whose value column is `water_level`, not `discharge`.

    ONE read, not two. This used to sniff the header with nrows=0 and then read
    the file again, which is free against a local disk and doubles the transfer
    against S3 -- two round trips per gauge, ~2,600 gauges a VPU. The columns are
    checked on the frame that was already read instead.

    `source` supplies the opener; without one `loc` is treated as a local path,
    which is what lets callers that have not been given a source still work.
    A None `loc` means the source could not place this gauge at all, which is
    the same outcome as an unreadable file and is reported the same way.
    """
    if loc is None:
        return None
    opener = source.open if source is not None else (lambda p: open(p, "rb"))
    try:
        with opener(loc) as fh:
            d = pd.read_csv(fh)
        if "discharge" not in d.columns or "datetime" not in d.columns:
            return None
        d = d[["datetime", "discharge"]]
        d["datetime"] = pd.to_datetime(d["datetime"], errors="coerce")
        d = d[d["datetime"].notna()]
    except Exception:
        return None
    if d.empty:
        return None
    d["datetime"] = d["datetime"].dt.floor("D")
    # Duplicate dates do occur; keep the first reading for the day.
    s = d.drop_duplicates("datetime").set_index("datetime")["discharge"]
    return s[s.notna()]


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def metrics_from_pair(sim: np.ndarray, obs: np.ndarray) -> dict:
    """Every paired-series metric, from one (sim, obs) pair of equal length.

    Called by pair_stats() for the whole record and by monthly_stats() once per
    calendar month, so a metric added here appears in both.

    Each metric is written only if its own divisor is non-zero, so a gauge with a
    flat or all-zero record still gets rmse and mae rather than being dropped:

        always                        n, mean_obs, mean_sim, sd_obs, sd_sim,
                                      rmse, mae
        if obs varies                 alpha, nse
        if obs and sim both vary      r, spearman
        if mean_obs != 0              beta, pbias_pct, nrmse, mae_rel
        if all of the above, and
        mean_sim != 0 and gamma
        is finite                     gamma, kge_2012

    "varies" is tested as max > min, not as sd > 0 -- see the note on the guards
    below for why the two are not interchangeable here.
    """
    out = {}
    n = sim.size
    if n < 2:
        return out

    mo, ms = float(obs.mean()), float(sim.mean())
    so, ss = float(obs.std(ddof=0)), float(sim.std(ddof=0))
    out.update(mean_obs=mo, mean_sim=ms, sd_obs=so, sd_sim=ss)

    # Whether a series varies is decided by comparing its extremes, NOT by
    # testing its standard deviation against zero. numpy computes the spread as
    # deviations about a computed mean, and that mean is often a rounding error
    # away from the true value, so a genuinely constant series can report a
    # spread of ~1e-16 instead of 0.
    #
    # max > min is an exact comparison on the stored values, so it cannot be
    # fooled by how the mean was summed, and it needs no tolerance to tune. It
    # also puts r and spearman on the same footing: rankdata() of a constant
    # series is genuinely constant, so spearman returned NaN where r returned
    # rounding noise, even though both were gated on the same condition.
    obs_varies = bool(obs.max() > obs.min())
    sim_varies = bool(sim.max() > sim.min())

    rmse = float(np.sqrt(np.mean((sim - obs) ** 2)))
    mae = float(np.mean(np.abs(sim - obs)))
    out.update(rmse=rmse, mae=mae)

    if obs_varies:
        out["alpha"] = ss / so                    # 0 is meaningful: no variability
        out["nse"] = 1.0 - (rmse / so) ** 2
    r = None
    if obs_varies and sim_varies:
        r = float(np.corrcoef(sim, obs)[0, 1])
        out["r"] = r
        # Spearman is Pearson on the ranks, with ties averaged. A constant series
        # has constant ranks, so it needs the same non-flat condition as r.
        out["spearman"] = float(np.corrcoef(rankdata(sim), rankdata(obs))[0, 1])

    if mo != 0:
        beta = ms / mo
        out["beta"] = beta
        out["pbias_pct"] = 100.0 * (beta - 1.0)
        out["nrmse"] = rmse / mo
        out["mae_rel"] = mae / mo
        if obs_varies and sim_varies and ms != 0:
            gamma = (ss / ms) / (so / mo)
            if np.isfinite(gamma):
                out["gamma"] = float(gamma)
                # r is a float here: it is assigned under exactly the
                # obs_varies and sim_varies condition this branch also requires.
                out["kge_2012"] = 1.0 - math.sqrt(
                    (r - 1) ** 2 + (gamma - 1) ** 2 + (beta - 1) ** 2)
    return out


def pair_stats(sim: np.ndarray, obs: np.ndarray) -> dict:
    """Whole-record metrics for one gauge. See the module docstring for the table.

    A thin wrapper over metrics_from_pair(), which is also what monthly_stats()
    uses, so the annual and per-month figures are computed by identical code.
    """
    out = {"n_pairs": sim.size}
    out.update(metrics_from_pair(sim, obs))
    return out

# Minimum annual maxima before a 2-year return level is estimated. The T=2 level
# is the median of the annual maximum distribution, so it is the best-determined
# quantile you can ask for -- but a median of fewer than this many values is not
# worth reporting.
MIN_YEARS_FOR_RETURN = 10


def contingency_stats(both: pd.DataFrame, sim_full: pd.Series,
                      obs_full: pd.Series) -> dict:
    """Flood-detection scores at each series' own 2-year return level.

    THE THRESHOLD. For each series independently, take the maximum flow in each
    calendar year and fit a Gumbel Type-I distribution by method of moments,
    evaluated at T=2. Annual maxima come from each series' WHOLE record inside
    the evaluation window, not from the days the two series happen to share.

    geoglows.analyze.gumbel1() is called directly rather than reimplemented, so
    the two cannot drift apart:

        x_T = -ln(-ln(1 - 1/T)) * std * 0.7797 + xbar - 0.45 * std

    where xbar and std are the mean and the POPULATION standard deviation
    (ddof=0) of the annual maxima. 0.7797 is sqrt(6)/pi and 0.45 is
    0.5772 * 0.7797, which is what makes it method of moments.

    Contingency table on daily exceedances (>= threshold), each series against
    its OWN threshold:
        a hits            both exceed
        b false alarms    model exceeds, gauge does not
        c misses          gauge exceeds, model does not
        d correct negs    neither

        pod  = a/(a+c)              probability of detection
        far  = b/(a+b)              false alarm ratio
        csi  = a/(a+b+c)            critical success index
        ets  = (a-ar)/(a+b+c-ar)    equitable threat score, ar = (a+b)(a+c)/n
        freq_bias = (a+b)/(a+c)
    """
    out = {}
    o = both["obs"]
    s = both["sim"]

    # Annual maxima by calendar year. A basin whose flood season spans the new year can
    # therefore have one event split across two years.
    ams_o = obs_full.groupby(obs_full.index.year).max().dropna()
    ams_s = sim_full.groupby(sim_full.index.year).max().dropna()
    n_years = min(len(ams_o), len(ams_s))    # both were just dropna()'d above
    out["n_years_ams"] = n_years
    if n_years < MIN_YEARS_FOR_RETURN:
        return out

    # Called from the geoglows package so this stays identical to RFS.
    from geoglows.analyze import gumbel1
    t2_o = float(gumbel1(2, float(ams_o.mean()), float(ams_o.std(ddof=0))))
    t2_s = float(gumbel1(2, float(ams_s.mean()), float(ams_s.std(ddof=0))))
    out["t2_obs"] = t2_o
    out["t2_sim"] = t2_s
    if not (t2_o > 0 and t2_s > 0):
        return out                       # an all-dry record has no flood level

    eo = (o >= t2_o).to_numpy()
    es = (s >= t2_s).to_numpy()
    a = int(np.sum(eo & es))
    b = int(np.sum(~eo & es))
    c = int(np.sum(eo & ~es))
    d = int(np.sum(~eo & ~es))
    n = a + b + c + d
    out.update(hits=a, false_alarms=b, misses=c, correct_neg=d)

    if a + c > 0:
        out["pod"] = a / (a + c)
        out["freq_bias"] = (a + b) / (a + c)
    if a + b > 0:
        out["far"] = b / (a + b)
    if a + b + c > 0:
        out["csi"] = a / (a + b + c)
        ar = (a + b) * (a + c) / n
        if (a + b + c - ar) != 0:
            out["ets"] = (a - ar) / (a + b + c - ar)
    return out


def loo_climatology(obs: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """Leave-one-year-out day-of-year climatology, aligned to `obs`.

    Each day is predicted by the mean of that day-of-year across every OTHER
    year, so the reference is never fitted to the day it is scored on. The
    vectorised form of that is (total_for_doy - this_value) / (count_for_doy - 1).

    Returns (clim, ok): clim[i] is the reference for obs[i], and ok[i] is False
    where that day-of-year occurs only once in the record, leaving nothing to
    average once the day itself is removed.

    Shared by skill_scores() and monthly_stats() so the monthly scores use the
    same reference as the annual one rather than a separately built copy.
    """
    doy = obs.index.dayofyear
    ov = obs.to_numpy()
    tot = obs.groupby(doy).sum().reindex(doy).to_numpy()
    cnt = obs.groupby(doy).size().reindex(doy).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        clim = (tot - ov) / (cnt - 1)
    return clim, np.isfinite(clim) & (cnt > 1)


def climatology_skill(sim: np.ndarray, obs: np.ndarray, ref: np.ndarray) -> dict:
    """SS = 1 - loss(model) / loss(reference), for squared and absolute error.

      > 0  the model beats the gauge's own seasonal cycle
      = 0  no better than knowing the time of year
      < 0  worse than knowing only the time of year

    The MSE and MAE versions can disagree substantially, because squaring weights
    the largest misses far more heavily. Both are stored; compare them rather
    than assuming they agree. Each is written only if its reference loss is
    non-zero.
    """
    out = {}
    mse_ref = float(np.mean((ref - obs) ** 2))
    if mse_ref > 0:
        out["ss_clim"] = 1.0 - float(np.mean((sim - obs) ** 2)) / mse_ref
    mae_ref = float(np.mean(np.abs(ref - obs)))
    if mae_ref > 0:
        out["ss_clim_mae"] = 1.0 - float(np.mean(np.abs(sim - obs))) / mae_ref
    return out


def skill_scores(both: pd.DataFrame) -> dict:
    """Skill of the simulation against the observed day-of-year climatology.

        SS = 1 - E_model / E_reference

    0 means "no better than the reference", 1 is perfect, negative is worse.

    The reference is the gauge's own day-of-year climatology.

    The climatology is built leave-one-year-out -- each day is predicted by the
    mean of that day-of-year across every *other* year -- so the reference is not
    fitted to the day it is scored on. It is not smoothed across neighbouring
    days, which would make it a slightly stronger benchmark.
    """
    clim, ok = loo_climatology(both["obs"])
    if ok.sum() < 365:
        return {}

    out = {"n_clim": int(ok.sum())}
    out.update(climatology_skill(both["sim"].to_numpy()[ok],
                                 both["obs"].to_numpy()[ok],
                                 clim[ok]))
    return out


# Minimum paired days within a calendar month before that month's metrics are
# reported. On a long record it trims almost nothing -- the median gauge-month on
# the full VPU 714 run holds ~1,590 days.

MIN_DAYS_PER_MONTH = 60


def monthly_stats(both: pd.DataFrame) -> dict:
    """Every whole-record metric, recomputed within each calendar month.

    Pools the same month across all years -- every January day against every
    January day.

    Columns are <metric>_m<MM> for MM = 01..12, matching the whole-record names.
    A month with fewer than MIN_DAYS_PER_MONTH paired days gets only n_m<MM>.

    WHAT A MONTHLY VALUE MEANS. Conditioning on one month compresses the flow
    range, so these are NOT slices of the annual figures. Every moment is taken
    from that month's own data, so alpha, beta and gamma are ratios of
    within-month statistics, and a monthly KGE' is built from those. Read them
    as within-month scores.

    ss_clim_m<MM> keeps the SAME leave-one-year-out day-of-year reference the
    annual score uses -- the reference is not rebuilt per month -- and simply
    scores it over that month's days.
    """
    out = {}
    mon = both.index.month

    # The leave-one-year-out climatology is built once over the whole record, so
    # that a month's reference is identical to the one the annual score used.
    o_all = both["obs"]
    doy = o_all.index.dayofyear
    ov_all = o_all.to_numpy()
    clim_all, clim_ok = loo_climatology(o_all)

    for m in range(1, 13):
        sel = mon == m
        n = int(sel.sum())
        out[f"n_m{m:02d}"] = n
        if n < MIN_DAYS_PER_MONTH:
            continue

        s = both["sim"].to_numpy("float64")[sel]
        o = both["obs"].to_numpy("float64")[sel]
        for k, v in metrics_from_pair(s, o).items():
            out[f"{k}_m{m:02d}"] = v

        # Skill against the shared annual climatology, scored on this month only.
        ok = sel & clim_ok
        if ok.sum() >= MIN_DAYS_PER_MONTH:
            out[f"n_clim_m{m:02d}"] = int(ok.sum())
            for k, v in climatology_skill(both["sim"].to_numpy("float64")[ok],
                                          ov_all[ok], clim_all[ok]).items():
                out[f"{k}_m{m:02d}"] = v
    return out


# --------------------------------------------------------------------------- #
# Decision mode: HydroSOS low-flow status
# --------------------------------------------------------------------------- #

# A calendar month is usable if at least this share of its days are present.
HYDROSOS_MIN_MONTH_PCT = 50.0
# Every calendar month needs MORE than this many usable instances, or the gauge
# is dropped: with fewer there are too few values to place a 10th-percentile
# breakpoint. Strictly greater, so 10 requires 11.
HYDROSOS_MIN_YEARS_PER_MONTH = 10
# Rank targets, and the category each one opens. Category 1 is the driest.
HYDROSOS_TARGET_RANKS = (0.10, 0.25, 0.75, 0.90)

# --- Decision 1 verdict ---------------------------------------------------- #
# PROVISIONAL. Chosen 2026-09-18; the reasoning, the measured consequences and
# the objection to the 0.33 cut are in DECISION_MODE.md. Changing a number here
# changes every verdict, so change it here and nowhere else.
#
# Category 1 is the driest 10% of each calendar month by construction, so a
# model that knows nothing still lands on 10% of the gauge's severe months by
# luck. RED is not a catch-rate threshold: it is the gauges where that luck
# cannot be ruled out, tested per gauge because a short record needs a far
# higher catch rate to prove anything than a long one does.
VERDICT_BASE_RATE = 0.10      # share of months that are category 1
VERDICT_ALPHA = 0.05          # one-sided; above this p-value the gauge is red
VERDICT_GREEN = 0.50          # catches at least half of severe dry months
VERDICT_GOOD = 0.33           # catches a third to a half

# -1 grey so the value is never null: a gauge with no verdict must still draw on
# the map rather than being filtered out as missing data.
VERDICT_GREY, VERDICT_RED, VERDICT_WEAK, VERDICT_GOOD_N, VERDICT_GREEN_N = -1, 0, 1, 2, 3
# "within what luck produces", not "no better than luck": the test is one-sided
# and cannot assert equality with luck. Gauges measurably WORSE than luck exist
# but are 0.0-0.3% of gauges across five VPUs, so they are not split out.
VERDICT_LABELS = {-1: "not enough data to judge",
                  0: "no - hits within what luck would produce",
                  1: "weak - beats luck, catches under a third",
                  2: "good - catches a third to a half",
                  3: "yes - catches at least half"}


def hydrosos_verdict(catch, p_luck) -> int:
    """Traffic-light verdict for the severe-dry decision.

    `p_luck` is the probability a model knowing nothing would match this many
    of the gauge's severe months or more. Above VERDICT_ALPHA the gauge is red:
    not "it scored badly" but "we cannot show this beats guessing".
    """
    if catch is None or p_luck is None:
        return VERDICT_GREY
    try:
        catch = float(catch); p_luck = float(p_luck)
    except (TypeError, ValueError):
        return VERDICT_GREY
    if not (np.isfinite(catch) and np.isfinite(p_luck)):
        return VERDICT_GREY
    if p_luck > VERDICT_ALPHA:
        return VERDICT_RED
    if catch >= VERDICT_GREEN:
        return VERDICT_GREEN_N
    if catch >= VERDICT_GOOD:
        return VERDICT_GOOD_N
    return VERDICT_WEAK


def hydrosos_categories(daily: pd.Series, usable: pd.Series) -> pd.Series:
    """HydroSOS 1-5 status per calendar month, following `statuscalc.py`.

    `daily` is a daily discharge series; `usable` is a boolean indexed by
    (year, month) saying which months clear the completeness rule. The
    reference window is the whole of `daily` -- the caller decides what that
    window is.

        1  <= p10      2  <= p25      3  <= p75      4  <= p90      5  > p90

    Monthly means are divided by the long-term average of their calendar month
    before ranking. That is a constant within a month, so it cannot reorder
    values or change a category; it is kept because the published method emits
    the normalised value and the thresholds alongside the category.

    Breakpoints are Weibull plotting positions, rank/(n+1), linearly
    interpolated between the ranks bracketing each target and clamped to the
    end values when a target falls outside the observed range.

    Returns a Series of Int64 categories indexed by (year, month), NA where the
    month was unusable.
    """
    key = [daily.index.year, daily.index.month]
    mean_flow = daily.groupby(key).mean()
    mean_flow.index.names = ["year", "month"]
    mean_flow = mean_flow.where(usable.reindex(mean_flow.index).fillna(False))

    months = mean_flow.index.get_level_values("month")
    lta = mean_flow.groupby(months).mean()
    pct = mean_flow / lta.reindex(months).to_numpy() * 100.0

    cat = pd.Series(pd.NA, index=mean_flow.index, dtype="Int64")
    for m in range(1, 13):
        vals = pct[months == m].dropna()
        if vals.empty:
            continue
        # rank/(n+1), then read the target ranks off the (rank, value) curve.
        ranks = vals.rank().to_numpy() / (len(vals) + 1)
        order = np.argsort(ranks, kind="stable")
        r_sorted = ranks[order]
        v_sorted = vals.to_numpy()[order]
        edges = [float(np.interp(t, r_sorted, v_sorted))
                 for t in HYDROSOS_TARGET_RANKS]
        # np.searchsorted with side="left" gives 1..5 against the four edges,
        # matching `<= p10 -> 1` ... `> p90 -> 5`.
        assigned = np.searchsorted(edges, vals.to_numpy(), side="left") + 1
        cat.loc[vals.index] = assigned.astype("int64")
    return cat


def _contingency(flag_s: np.ndarray, flag_o: np.ndarray, prefix: str) -> dict:
    """2x2 scores for one boolean band, named `<prefix>_*`.

    Same quantities as contingency_stats(); ets carries the chance correction,
    ar = (a+b)(a+c)/n, so its sign is the better-than-chance test.
    """
    a = int(np.sum(flag_o & flag_s))
    b = int(np.sum(~flag_o & flag_s))
    c = int(np.sum(flag_o & ~flag_s))
    d = int(np.sum(~flag_o & ~flag_s))
    n = a + b + c + d
    out = {f"{prefix}_hits": a, f"{prefix}_false_alarms": b,
           f"{prefix}_misses": c, f"{prefix}_correct_neg": d}
    if a + c > 0:
        out[f"{prefix}_pod"] = a / (a + c)
        out[f"{prefix}_freq_bias"] = (a + b) / (a + c)
    if a + b > 0:
        out[f"{prefix}_far"] = b / (a + b)
    if a + b + c > 0:
        out[f"{prefix}_csi"] = a / (a + b + c)
        ar = (a + b) * (a + c) / n
        if (a + b + c - ar) != 0:
            out[f"{prefix}_ets"] = (a - ar) / (a + b + c - ar)
    return out


def _kappa(cs: np.ndarray, co: np.ndarray, linear: bool) -> float | None:
    """Cohen's kappa over categories 1-5; linear disagreement weights optional.

    Chance uses the OBSERVED marginals, which is standard kappa. Because each
    series is binned against its own record the marginals sit near
    10/15/50/15/10 by construction, so p_e should land near 0.315; using the
    observed ones rather than assuming that lets a deviation show up instead of
    being hidden.
    """
    k = 5
    obs = np.zeros((k, k), dtype="float64")
    for i, j in zip(co - 1, cs - 1):
        obs[i, j] += 1
    n = obs.sum()
    if n == 0:
        return None
    # Agreement weights: 1 on the diagonal, 0 at maximum disagreement. Identity
    # for the unweighted case, so p_o is the plain match rate.
    idx = np.arange(k)
    w = (1.0 - np.abs(idx[:, None] - idx[None, :]) / (k - 1)) if linear \
        else np.eye(k)
    exp = np.outer(obs.sum(axis=1), obs.sum(axis=0)) / n
    p_o = float((w * obs).sum() / n)
    p_e = float((w * exp).sum() / n)
    if p_e >= 1.0:
        return None
    return (p_o - p_e) / (1.0 - p_e)


# --- Decision: is the annual volume of water representative? ---------------- #
# The ONLY decision here whose bands come from published literature. Moriasi et
# al. (2007), Table 4, Transactions of the ASABE 50(3):885-900 -- the standard
# performance ratings for streamflow in hydrologic model evaluation. Every other
# decision in this file has bands that were chosen; these were not.
#
# PBIAS = 100 * sum(sim - obs) / sum(obs), computed on daily paired values.
# Measured on VPU 714: the time step does not matter (daily 10.45%, monthly
# 10.37%, within 1.5 percentage points at every gauge, because PBIAS is a ratio
# of sums) and taking the median of each year's bias instead moves only 1.7% of
# gauges across a band. The standard statistic is used so the published bands
# stay valid.
#
# Scored on the GLOBALLY BIAS-CORRECTED series, not the raw model: the raw model
# is 66% unsatisfactory and correction takes that to 27%. Correction is what
# makes this question answerable at all.
VOLUME_VERY_GOOD = 10.0       # |PBIAS| below this
VOLUME_GOOD = 15.0
VOLUME_SATISFACTORY = 25.0    # above this is unsatisfactory

VOLUME_GREY, VOLUME_BAD, VOLUME_OK, VOLUME_GOOD_N, VOLUME_BEST = -1, 0, 1, 2, 3
VOLUME_LABELS = {-1: "no corrected series available",
                 0: "unsatisfactory - |PBIAS| over 25%",
                 1: "satisfactory - 15 to 25%",
                 2: "good - 10 to 15%",
                 3: "very good - under 10%"}


def volume_verdict(pbias) -> int:
    """Moriasi et al. (2007) streamflow performance rating from PBIAS."""
    try:
        p = abs(float(pbias))
    except (TypeError, ValueError):
        return VOLUME_GREY
    if not np.isfinite(p):
        return VOLUME_GREY
    if p < VOLUME_VERY_GOOD:
        return VOLUME_BEST
    if p < VOLUME_GOOD:
        return VOLUME_GOOD_N
    if p < VOLUME_SATISFACTORY:
        return VOLUME_OK
    return VOLUME_BAD


def corrected_series(river_id: int, sim: pd.Series) -> pd.Series | None:
    """GEOGLOWS global bias correction for one reach, or None if unusable.

    geoglows.bias.sfdc_bias_correction divides the simulation by a scalar read
    from the published SFDC table. Where that table holds a zero the result is
    inf, which is a defect in the published correction rather than in the model
    or the gauge -- and it is not rare: measured across five VPUs the unusable
    share runs from 4% (VPU 209) to 34% (VPU 208).

    Note the function returns ONE column, overwriting the input, despite a
    docstring promising two ("Simulated flow, Bias Corrected Simulation flow").
    """
    try:
        from geoglows.bias import sfdc_bias_correction
        # Silenced NARROWLY, around this call only. A zero scalar makes numpy
        # emit "invalid value encountered in divide" per reach and per month,
        # which buries the real output in thousands of lines and reads like a
        # crash. The condition is not ignored -- it is exactly what the
        # isfinite check below catches, and those reaches are reported as
        # unusable rather than silently dropped.
        with np.errstate(divide="ignore", invalid="ignore"):
            out = sfdc_bias_correction(sim.to_frame(name="sim"), int(river_id))
    except Exception:
        return None
    s = out[out.columns[-1]]
    if not np.isfinite(s.to_numpy()).all():
        return None
    return s


def volume_stats(sim_cor: pd.Series, obs: pd.Series) -> dict:
    """PBIAS of the bias-corrected series against the gauge, and its rating.

      vol_pbias    100 * sum(sim - obs) / sum(obs), on paired days
      vol_n_days   paired days it was computed over
      vol_verdict  -1 grey, 0 unsatisfactory, 1 satisfactory, 2 good, 3 very good
    """
    both = pd.concat([sim_cor.rename("sim"), obs.rename("obs")],
                     axis=1, join="inner").dropna()
    if len(both) < 365:
        return {}
    tot = float(both["obs"].sum())
    if not (tot > 0):
        return {}
    p = 100.0 * (float(both["sim"].sum()) - tot) / tot
    return {"vol_pbias": p, "vol_n_days": int(len(both)),
            "vol_verdict": volume_verdict(p)}


# --- Decision: does the model show a flood when the river floods? ----------- #
# Each series is thresholded at ITS OWN 2-year level, so flood MAGNITUDE divides
# out -- this establishes whether a flood shows up and when, not whether it was
# the right size. The magnitude question was measured and abandoned: proving the
# model's flood levels per gauge needs ~145 years of record and none has 100.
#
# Scored on declustered EVENTS, not days: a flood spans ~2.9 days, so daily
# counts would triple the sample and break independence.
#
# FLOOD_SEP must be at least 2*FLOOD_WINDOW+1, or two neighbouring observed
# floods have overlapping match windows and one modelled flood can be credited
# to both. At 7 and 3 the windows exactly touch.
FLOOD_RP = 2                  # return period; CSI falls by half at 5yr and again by 10yr
FLOOD_WINDOW = 3              # a match counts within +/- this many days
FLOOD_SEP = 7                 # days two exceedances must be apart to be separate floods
FLOOD_ALPHA = 0.05            # one-sided; above this p-value the gauge is red
# 0.50 is the point where hits equal misses plus false alarms -- the forecast
# gets as much right as it gets wrong. It is arithmetic, not a published band:
# CSI has no standard skill classification, which was searched for and does not
# exist. Measured on 747 gauges across 5 VPUs, NO gauge reaches it. The empty
# band is deliberate -- it is a fixed reference that will not drift when a
# different model run is scored, unlike a cut taken from this run's spread.
FLOOD_STRONG = 0.50
FLOOD_MIDDLE = 0.19           # pooled median over those 747 gauges; descriptive only

FLOOD_GREY, FLOOD_POOR, FLOOD_WEAK, FLOOD_GOOD, FLOOD_STRONG_N = -1, 0, 1, 2, 3
FLOOD_LABELS = {-1: "not enough floods to judge",
                0: "cannot be shown to beat luck",
                1: "beats luck, below the median gauge",
                2: "above the median gauge",
                3: "as many hits as misses and false alarms combined"}


def return_level(series: pd.Series, T: float) -> float:
    """T-year level from a Gumbel MoM fit to calendar-year maxima.

    Same fit contingency_stats() uses, called through geoglows.analyze.gumbel1
    so it cannot drift from RFS. Annual maxima come from the series' WHOLE
    record in the window, not the paired days -- see KNOWN_ISSUES P.
    """
    ams = series.groupby(series.index.year).max().dropna()
    if len(ams) < MIN_YEARS_FOR_RETURN:
        return float("nan")
    from geoglows.analyze import gumbel1
    return float(gumbel1(T, float(ams.mean()), float(ams.std(ddof=0))))


def _flood_events(flag: np.ndarray, sep: int = FLOOD_SEP) -> np.ndarray:
    """Start index of each separate flood: runs of exceedance, declustered."""
    starts = np.flatnonzero(np.diff(np.r_[0, flag.astype(np.int8)]) == 1)
    if len(starts) == 0:
        return starts
    keep = [starts[0]]
    for s in starts[1:]:
        if s - keep[-1] >= sep:
            keep.append(s)
    return np.array(keep)


def _flood_matched(starts: np.ndarray, other: np.ndarray,
                   w: int = FLOOD_WINDOW) -> int:
    """How many of `starts` have the other series flagged within +/- w days."""
    n = len(other)
    return sum(1 for s in starts
               if other[max(0, s - w):min(n, s + w + 1)].any())


def flood_stats(both: pd.DataFrame, t_obs: float, t_sim: float) -> dict:
    """Does a modelled flood turn up when the gauge floods?

      fl_n_obs     floods the gauge recorded
      fl_n_sim     floods the model produced
      fl_hits      gauge floods with a modelled flood within +/- 3 days
      fl_false     modelled floods with no observed counterpart
      fl_csi       hits / (hits + misses + false alarms)
      fl_p_luck    chance of this many hits or more from a model with no skill
      fl_verdict   -1 grey, 0 poor, 1 weak, 2 good, 3 strong

    CSI rather than the plain hit rate because the hit rate can be inflated by
    flooding more often, and CSI cannot: it carries the false alarms in its
    denominator. On VPU 714 the two rank gauges at 0.96 correlation, but at the
    1.4% of gauges that over-flood the hit rate says 0.24 where CSI says 0.11.
    """
    out = {}
    if not (np.isfinite(t_obs) and np.isfinite(t_sim) and t_obs > 0 and t_sim > 0):
        return out
    fo = (both["obs"] >= t_obs).to_numpy()
    fs = (both["sim"] >= t_sim).to_numpy()
    eo, es = _flood_events(fo), _flood_events(fs)
    out["fl_n_obs"], out["fl_n_sim"] = int(len(eo)), int(len(es))
    if len(eo) < 5 or len(es) == 0:
        return out                      # too few floods to say anything

    hits = _flood_matched(eo, fs)
    hits_sim = _flood_matched(es, fo)
    false_alarms = len(es) - hits_sim
    misses = len(eo) - hits
    out["fl_hits"], out["fl_false"] = int(hits), int(false_alarms)
    den = hits + false_alarms + misses
    if den == 0:
        return out
    csi = hits / den
    out["fl_csi"] = float(csi)

    # A model with no skill still lands in some windows. Its per-flood chance is
    # 1-(1-p)^(2w+1) where p is how often it floods -- so a model that floods
    # more has a HIGHER bar, which is the point of testing per gauge.
    p = float(fs.mean())
    chance = 1.0 - (1.0 - p) ** (2 * FLOOD_WINDOW + 1)
    from scipy.stats import binom
    out["fl_p_luck"] = float(binom.sf(hits - 1, len(eo), chance))

    if out["fl_p_luck"] > FLOOD_ALPHA:
        out["fl_verdict"] = FLOOD_POOR
    elif csi >= FLOOD_STRONG:
        out["fl_verdict"] = FLOOD_STRONG_N
    elif csi >= FLOOD_MIDDLE:
        out["fl_verdict"] = FLOOD_GOOD
    else:
        out["fl_verdict"] = FLOOD_WEAK
    return out


# --- Decision: does the model tell wet days from dry days? ------------------ #
# Scored from the `spearman` column the metric table already carries -- daily
# rank correlation between modelled and observed flow -- so this needs no extra
# computation and no re-run. serve.py derives the verdict at display time,
# which is why changing a band here takes effect on the next page load.
#
# PROVISIONAL BANDS, chosen from the VPU 714 distribution (p10 0.32, median
# 0.57, p90 0.71) and not anchored to anything. See DECISION_MODE.md for the
# alternative that was measured but not taken: a per-gauge benchmark of what
# the seasonal cycle ALONE scores, which the model beats at 65% of gauges and
# which would make the bottom band mean something derived rather than chosen.
#
# The user was shown that seasonality drives roughly 0.506 of the median 0.569
# and accepted it: for someone asking when a river runs high or low, getting
# the seasonal timing right is part of the answer.
WETDRY_STRONG = 0.70
WETDRY_GOOD = 0.50
WETDRY_WEAK = 0.30

WETDRY_GREY, WETDRY_POOR, WETDRY_WEAK_N, WETDRY_GOOD_N, WETDRY_STRONG_N = -1, 0, 1, 2, 3
WETDRY_LABELS = {-1: "no value",
                 0: "poor - under 0.30",
                 1: "weak - 0.30 to 0.50",
                 2: "good - 0.50 to 0.70",
                 3: "strong - 0.70 and above"}


def wetdry_verdict(rho) -> int:
    """Band the daily rank correlation between modelled and observed flow."""
    try:
        rho = float(rho)
    except (TypeError, ValueError):
        return WETDRY_GREY
    if not np.isfinite(rho):
        return WETDRY_GREY
    if rho >= WETDRY_STRONG:
        return WETDRY_STRONG_N
    if rho >= WETDRY_GOOD:
        return WETDRY_GOOD_N
    if rho >= WETDRY_WEAK:
        return WETDRY_WEAK_N
    return WETDRY_POOR


# --- Decision 3: is the river getting wetter or drier? ---------------------- #
# PROVISIONAL, and three choices here are inherited from the analysis the
# four states were measured on rather than separately agreed. All are one-line
# changes; see DECISION_MODE.md.
#
#   * the variable is ANNUAL MEAN flow -- "wetter or drier" in the volume sense.
#     Trend in annual MAXIMUM (are floods worsening) or annual MINIMUM (are
#     droughts deepening) are different questions with different answers.
#   * a year counts only with TREND_MIN_DAYS paired days, so a part-year cannot
#     masquerade as a dry or wet one. Measured on VPU 714, raising this from 300
#     to 364 moved kappa 0.040 -> 0.055 and left the marginals unchanged.
#   * plain Mann-Kendall OVER-REJECTS on autocorrelated series, so both series
#     likely find more trends than are real. Prewhitening is NOT implemented.
TREND_MIN_DAYS = 300          # paired days a year needs to be used
TREND_MIN_YEARS = 30          # usable years a gauge needs for any verdict
TREND_ALPHA = 0.05            # Mann-Kendall significance

TREND_GREY, TREND_INVENTS, TREND_MISSES, TREND_NONE, TREND_AGREES = -1, 0, 1, 2, 3
TREND_LABELS = {-1: "not enough data to judge",
                0: "invents a trend the gauge does not show",
                1: "misses a trend the gauge does show",
                2: "neither finds a trend",
                3: "agrees on the direction"}


def _mk_sign(y: np.ndarray) -> int:
    """Mann-Kendall direction: +1 wetting, -1 drying, 0 no significant trend."""
    from scipy.stats import kendalltau
    tau, p = kendalltau(np.arange(len(y)), y)
    if not np.isfinite(p) or p > TREND_ALPHA:
        return 0
    return 1 if tau > 0 else -1


def trend_stats(both: pd.DataFrame) -> dict:
    """Do model and gauge agree on whether the river is changing?

    Annual means are taken from the PAIRED days only, so both series describe
    the same days of the same years and a difference cannot come from one of
    them covering a period the other does not.

      tr_n_years   usable years
      tr_obs       gauge direction   +1 wetting, 0 none, -1 drying
      tr_sim       model direction
      tr_verdict   -1 grey, 0 invents, 1 misses, 2 neither, 3 agrees

    "Neither finds a trend" is NOT a pass. It is the model being silent where
    there was nothing to detect, and on VPU 714 it is half of all gauges -- a
    plain match/no-match split would score those as successes and read 55%
    correct off what is mostly mutual silence.
    """
    g = both.groupby(both.index.year)
    size = g.size()
    keep = size[size >= TREND_MIN_DAYS].index
    out = {"tr_n_years": int(len(keep))}
    if len(keep) < TREND_MIN_YEARS:
        return out
    so = _mk_sign(g["obs"].mean().loc[keep].to_numpy())
    ss = _mk_sign(g["sim"].mean().loc[keep].to_numpy())
    out["tr_obs"], out["tr_sim"] = so, ss
    if so == 0 and ss == 0:
        v = TREND_NONE
    elif so != 0 and so == ss:
        v = TREND_AGREES
    elif so != 0 and ss == 0:
        v = TREND_MISSES
    else:
        # Model asserts a trend the gauge does not support: either the gauge is
        # flat, or the two point opposite ways. Both are the model speaking out
        # of turn, so they share a state.
        v = TREND_INVENTS
    out["tr_verdict"] = v
    return out


def hydrosos_stats(both: pd.DataFrame) -> dict:
    """Agreement between modelled and observed HydroSOS low-flow status.

    Both series are categorised independently but identically, over the paired
    record -- so the reference window is the model/gauge overlap and both are
    ranked on exactly the same months. Monthly means come from paired days, so
    a day missing from either series is absent from both monthly means.

      hs_n_months        months compared
      hs_min_n_month     usable instances of the scarcest calendar month; the
                         binding constraint on every breakpoint
      hs_agree           share of months in the same category
      hs_kappa           linearly weighted kappa over the 5 categories
      hs_kappa_unw       unweighted kappa: exact category match only

      hs_dry_*           2x2 scores for "dry or worse", category <= 2
      hs_xdry_*          2x2 scores for "extremely dry", category == 1

    A gauge whose scarcest calendar month has HYDROSOS_MIN_YEARS_PER_MONTH or
    fewer usable instances returns hs_min_n_month alone and is dropped by the
    caller.
    """
    idx = both.index
    key = [idx.year, idx.month]
    present = both["sim"].groupby(key).size()
    present.index.names = ["year", "month"]
    days_in_month = pd.Series(
        [pd.Period(year=y, month=m, freq="M").days_in_month
         for y, m in present.index],
        index=present.index, dtype="float64")
    usable = (present / days_in_month * 100.0) >= HYDROSOS_MIN_MONTH_PCT

    per_month = usable[usable].groupby(
        usable[usable].index.get_level_values("month")).size()
    # A calendar month absent altogether counts as zero, not as missing.
    min_n = int(per_month.reindex(range(1, 13)).fillna(0).min())
    out = {"hs_min_n_month": min_n}
    if min_n <= HYDROSOS_MIN_YEARS_PER_MONTH:
        return out

    cat_s = hydrosos_categories(both["sim"], usable)
    cat_o = hydrosos_categories(both["obs"], usable)
    paired = pd.concat([cat_s.rename("s"), cat_o.rename("o")],
                       axis=1, join="inner").dropna()
    out["hs_n_months"] = int(len(paired))
    if paired.empty:
        return out

    cs = paired["s"].to_numpy("int64")
    co = paired["o"].to_numpy("int64")
    out["hs_agree"] = float(np.mean(cs == co))
    kw = _kappa(cs, co, linear=True)
    ku = _kappa(cs, co, linear=False)
    if kw is not None:
        out["hs_kappa"] = kw
    if ku is not None:
        out["hs_kappa_unw"] = ku
    out.update(_contingency(cs <= 2, co <= 2, "hs_dry"))
    out.update(_contingency(cs == 1, co == 1, "hs_xdry"))

    # Could this gauge's hit count have come from luck? Exact binomial, not the
    # normal approximation: a gauge with 12 severe months expects 1.2 hits by
    # chance, where the normal approximation is unreliable and too lenient --
    # it asks for a catch rate of 0.24 there against the exact test's 0.33.
    k = out.get("hs_xdry_hits", 0) + out.get("hs_xdry_misses", 0)
    a = out.get("hs_xdry_hits", 0)
    out["hs_xdry_n_events"] = int(k)
    if k > 0:
        from scipy.stats import binom
        out["hs_xdry_p_luck"] = float(binom.sf(a - 1, k, VERDICT_BASE_RATE))
        out["hs_verdict"] = hydrosos_verdict(out.get("hs_xdry_pod"),
                                             out["hs_xdry_p_luck"])
    return out


def fetch_corrections(river_ids, model: pd.DataFrame) -> dict:
    """Bias-corrected series for every reach, fetched in parallel.

    Network-bound, one request per reach, so this is threaded rather than run
    inside the gauge loop: measured 0.54s a reach serially against 0.21s at
    twelve workers, i.e. ~23 minutes against ~9 for a full VPU.
    """
    ids = [int(r) for r in river_ids if int(r) in model.columns]
    # Said up front, with an estimate: this is the one step that goes to the
    # network, it is minutes not seconds, and it prints nothing while it runs.
    # Without a warning it looks like the script has hung.
    secs = len(ids) * 0.21
    eta = (f"{secs:.0f} seconds" if secs < 90 else
           f"{secs/60:.0f} minutes")
    print(f"      bias correction: {len(ids):,} reaches over the network, "
          f"{GAUGE_WORKERS} workers")
    print(f"      roughly {eta}, and it prints nothing until it finishes")
    t0 = time.time()
    out, bad = {}, 0
    with ThreadPoolExecutor(max_workers=GAUGE_WORKERS) as pool:
        for rid, s in zip(ids, pool.map(
                lambda r: corrected_series(r, model[r]), ids)):
            if s is None:
                bad += 1
            else:
                out[rid] = s
    print(f"      corrected {len(out):,} in {(time.time()-t0)/60:.1f} min; "
          f"{bad:,} unusable because the published SFDC table holds a zero "
          f"for them -- a gap in the correction data, not in your gauges")
    return out


def compute_metrics(gauges: pd.DataFrame, model: pd.DataFrame,
                    min_years: float, source=None,
                    mode: str = "statistic",
                    corrections: dict | None = None) -> pd.DataFrame:
    """Pair each gauge against its reach and compute the metric table."""
    # The threshold counts PAIRED DAYS, not elapsed years: a gauge reporting
    # sparsely for thirty years can still fail it. Print the day count so the
    # filter cannot be misread as a span. See KNOWN_ISSUES section Q.
    min_days = int(round(min_years * 365.25))
    print(f"[3/5] pairing {len(gauges)} gauges "
          f"(minimum {min_days:,} paired days = {min_years}y)")

    # Reads run ahead of the arithmetic on a pool, because against S3 each one
    # is ~0.2s of latency and there are thousands. executor.map keeps them in
    # input order and streams rather than materialising every series at once, so
    # the loop below, the progress counter and the skip tallies are unchanged --
    # and a local run, where reads are already instant, is unaffected.
    pool = ThreadPoolExecutor(max_workers=GAUGE_WORKERS)
    try:
        series = pool.map(load_gauge_series, gauges["loc"],
                          [source] * len(gauges))

        rows, n_stage, n_short, n_empty, n_sparse = [], 0, 0, 0, 0
        for i, (g, obs) in enumerate(
                zip(gauges.itertuples(index=False), series), start=1):
            if i % 500 == 0:
                print(f"      {i}/{len(gauges)}")

            if obs is None:
                n_stage += 1
                continue
            if g.final_river_id not in model.columns:
                n_empty += 1
                continue

            sim = model[g.final_river_id]
            both = pd.concat([sim.rename("sim"), obs.rename("obs")], axis=1,
                             join="inner").dropna()
            if len(both) < min_days:
                n_short += 1
                continue

            st = {"n_pairs": len(both)}
            if mode in ("statistic", "both"):
                st.update(pair_stats(both["sim"].to_numpy("float64"),
                                     both["obs"].to_numpy("float64")))
                st.update(skill_scores(both))
                # Thresholds from each series' whole record in the window; the
                # contingency counts below still use the paired days only.
                # sim is already indexed on model.index; only obs needs clipping to it.
                st.update(contingency_stats(both, sim.dropna(),
                                            obs.reindex(model.index).dropna()))
                st.update(monthly_stats(both))
            if mode in ("decision", "both"):
                st.update(hydrosos_stats(both))
                st.update(trend_stats(both))
                # Whole-record thresholds, as contingency_stats uses, so the
                # 2-year level is one number throughout the file.
                st.update(flood_stats(
                    both,
                    return_level(obs.reindex(model.index).dropna(), FLOOD_RP),
                    return_level(sim.dropna(), FLOOD_RP)))
                # Volume is the one decision scored on the CORRECTED series;
                # the raw model is 66% unsatisfactory on these bands.
                if corrections is not None:
                    cs = corrections.get(int(g.final_river_id))
                    if cs is not None:
                        st.update(volume_stats(cs, obs))
                # A gauge answering neither decision is dropped only when the
                # decisions are all that is being written. Under "both" it is
                # kept -- it still has statistics -- and filtered out when the
                # decision table is split off. The two decisions ask different
                # things of the record, so a gauge can answer one and not the
                # other, and dropping it for failing either would silently
                # shrink the other's map.
                if (mode == "decision" and "hs_verdict" not in st
                        and "tr_verdict" not in st and "fl_verdict" not in st):
                    n_sparse += 1
                    continue
            # Every gauge attribute that reaches the parquet is listed here. A
            # column kept by build_gauge_table() but missing from this call is
            # silently dropped, so the two lists have to be changed together.
            st.update(final_river_id=g.final_river_id, gauge_id=g.gauge_id,
                      fname=g.fname, latitude=g.latitude, longitude=g.longitude,
                      strmOrder=g.strmOrder, USContArea=g.USContArea,
                      koppen=getattr(g, "koppen", None), ISO_A3=g.ISO_A3,
                      river_name=getattr(g, "river_name", None),
                      first_day=both.index.min(), last_day=both.index.max())
            rows.append(st)
    finally:
        # A SystemExit or Ctrl-C mid-loop otherwise waits on the pool's queued
        # reads before the message appears.
        pool.shutdown(wait=False, cancel_futures=True)

    print(f"      kept {len(rows)}  |  skipped: {n_stage} stage-only, "
          f"{n_short} under {min_days:,} paired days, {n_empty} absent from the "
          f"model array"
          + (f", {n_sparse} without more than {HYDROSOS_MIN_YEARS_PER_MONTH} "
             f"usable instances of every calendar month" if mode == "decision"
             else ""))

    m = pd.DataFrame(rows)
    if m.empty:
        sys.exit("no gauges survived the filters")

    # One reach can carry several gauges; keep the longest paired record so a
    # single reach is not scored twice in the aggregate statistics.
    before = len(m)
    m = (m.sort_values("n_pairs", ascending=False)
           .drop_duplicates("final_river_id", keep="first")
           .reset_index(drop=True))
    if before != len(m):
        print(f"      dropped {before-len(m)} duplicate gauges on shared reaches")
    return m


# --------------------------------------------------------------------------- #
# Map
# --------------------------------------------------------------------------- #

def plot_kge_map(m: pd.DataFrame, vpu: int, out_png: str,
                 date_start: str, date_end: str, run_label: str) -> None:
    """Scatter KGE' at the gauge locations.

    Colour rules: a diverging red-gray-blue scale centred on 0 and clipped to
    +/-COLOR_LIMIT. Clipping is what keeps the scale readable -- KGE' is
    unbounded below, so without it a handful of very poor gauges would flatten
    everything else into the middle.

    run_label names the discharge being scored and goes in the footer. The PNG is
    the one output that travels without a run.json beside it, so it is the one
    that most needs to say what it is.
    """
    import matplotlib
    matplotlib.use("Agg")
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap, Normalize

    col = KGE_COL
    d = m[np.isfinite(m[col])].copy()
    print(f"[5/5] mapping {col} for {len(d)} gauges -> {out_png}")

    cmap = LinearSegmentedColormap.from_list("kge_div", DIVERGING)
    norm = Normalize(vmin=-COLOR_LIMIT, vmax=COLOR_LIMIT)
    n_clipped = int((d[col] < -COLOR_LIMIT).sum())

    lon0, lon1 = d.longitude.min() - 1.5, d.longitude.max() + 1.5
    lat0, lat1 = d.latitude.min() - 1.0, d.latitude.max() + 1.0

    fig = plt.figure(figsize=(13, 8.5), facecolor=SURFACE)
    ax = plt.axes(projection=ccrs.PlateCarree(), facecolor=WATER)
    ax.set_extent([lon0, lon1, lat0, lat1], crs=ccrs.PlateCarree())

    # Recessive basemap: the data is the subject, geography is orientation only.
    ax.add_feature(cfeature.LAND.with_scale("50m"), facecolor=LAND, zorder=0)
    ax.add_feature(cfeature.OCEAN.with_scale("50m"), facecolor=WATER, zorder=0)
    ax.add_feature(cfeature.LAKES.with_scale("50m"), facecolor=WATER,
                   edgecolor=HAIRLINE, linewidth=0.4, zorder=1)
    ax.add_feature(cfeature.STATES.with_scale("50m"), edgecolor=HAIRLINE,
                   linewidth=0.5, zorder=2)
    ax.add_feature(cfeature.BORDERS.with_scale("50m"), edgecolor=INK_MUTED,
                   linewidth=0.6, zorder=2)
    ax.add_feature(cfeature.COASTLINE.with_scale("50m"), edgecolor=INK_MUTED,
                   linewidth=0.6, zorder=2)

    # Draw worst-first so poor gauges are never hidden under good ones by
    # overplotting. With a diverging scale the poor end is already red, so the
    # separate "below no-skill" marker is no longer needed.
    ds = d.sort_values(col, ascending=False)
    ax.scatter(ds.longitude, ds.latitude, c=ds[col].clip(-COLOR_LIMIT, COLOR_LIMIT),
               cmap=cmap, norm=norm, s=18, linewidth=0.3, edgecolor=SURFACE,
               transform=ccrs.PlateCarree(), zorder=3)

    gl = ax.gridlines(draw_labels=True, linewidth=0.4, color=HAIRLINE, alpha=0.9)
    gl.top_labels = gl.right_labels = False
    gl.xlabel_style = gl.ylabel_style = {"size": 8, "color": INK_MUTED}

    label = KGE_LABEL
    cb = fig.colorbar(plt.cm.ScalarMappable(norm=norm, cmap=cmap), ax=ax,
                      orientation="vertical", fraction=0.026, pad=0.02,
                      extend="min")
    cb.set_label(label, size=9.5, color=INK_SECONDARY)
    cb.ax.tick_params(labelsize=8, colors=INK_MUTED, length=2)
    cb.outline.set_visible(False)

    # Mark the true no-skill line. It is not 0, and the difference matters:
    # everything between -0.41 and 0 still beats the mean-flow benchmark.
    cb.ax.axhline(KGE_NO_SKILL, color=INK_PRIMARY, linewidth=1.1)
    cb.ax.text(1.9, KGE_NO_SKILL, f"  {KGE_NO_SKILL}  no skill", va="center",
               ha="left", fontsize=7.4, color=INK_SECONDARY,
               transform=cb.ax.get_yaxis_transform())

    med = d[col].median()
    frac_bad = 100.0 * (d[col] < KGE_NO_SKILL).mean()
    frac_good = 100.0 * (d[col] >= 0.5).mean()
    med_yrs = (d["n_pairs"] / 365.25).median()

    ax.set_title(f"{label} at gauge locations — VPU {vpu}",
                 fontsize=15, color=INK_PRIMARY, pad=22, loc="left")
    clip_note = f"  ·  {n_clipped} gauges below −1 shown at the scale floor" if n_clipped else ""
    ax.text(0.0, 1.028,
            f"{len(d):,} gauges  ·  median {med:.2f}  ·  "
            f"{frac_good:.0f}% at or above 0.50  ·  "
            f"{frac_bad:.0f}% below the no-skill benchmark  ·  "
            f"median {med_yrs:.0f} yr of paired record per gauge{clip_note}",
            transform=ax.transAxes, fontsize=9.5, color=INK_SECONDARY)

    fig.text(0.005, 0.012,
             f"Model: {run_label} (daily mean). Observed: daily mean gauge discharge. "
             "Each gauge is scored only on the days where that gauge and the model both have data,\n"
             "so the paired record differs per gauge. Points drawn worst-first so poor gauges are "
             f"not hidden by overplotting. KGE = {KGE_NO_SKILL} is the mean-flow benchmark "
             f"(Knoben et al. 2019), not 0.  Evaluation window {date_start} to {date_end}.",
             fontsize=7.2, color=INK_MUTED, va="bottom")

    fig.savefig(out_png, dpi=170, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)


# --------------------------------------------------------------------------- #

def summarize_decision(m: pd.DataFrame) -> None:
    """Verdict counts for the severe-dry decision.

    Percentages are of every gauge that reached the decision run, so grey is
    counted rather than hidden -- it is a third of the map on some VPUs and
    quoting shares of the scored gauges alone would understate it.
    """
    print(f"\n{'='*62}\nDoes the model identify severe low flow?  {len(m)} gauges"
          f"\n{'='*62}")
    print(f"  green at catch >= {VERDICT_GREEN:.2f}, good >= {VERDICT_GOOD:.2f}, "
          f"red = luck not ruled out at p <= {VERDICT_ALPHA:.2f}\n")
    if "hs_verdict" not in m.columns:
        print("  no verdicts in this table")
        return
    v = m["hs_verdict"].fillna(VERDICT_GREY).astype(int)
    for code in (VERDICT_GREEN_N, VERDICT_GOOD_N, VERDICT_WEAK,
                 VERDICT_RED, VERDICT_GREY):
        n = int((v == code).sum())
        print(f"    {VERDICT_LABELS[code]:42s} {n:6d}  {100*n/len(v):5.1f}%")
    scored = m.loc[v != VERDICT_GREY, "hs_xdry_pod"].dropna()
    if not scored.empty:
        print(f"\n  catch rate where scored: median {scored.median():.2f}, "
              f"p05 {scored.quantile(.05):.2f}, p95 {scored.quantile(.95):.2f}")
    if "hs_xdry_n_events" in m.columns:
        k = m["hs_xdry_n_events"].dropna()
        k = k[k > 0]
        if not k.empty:
            print(f"  severe months judged on: median {k.median():.0f}, "
                  f"min {k.min():.0f}, max {k.max():.0f}")

    if "vol_verdict" in m.columns:
        print(f"\n{'='*62}\nIs the annual volume of water representative?"
              f"\n{'='*62}")
        print("  PBIAS of the GEOGLOWS bias-corrected series against the gauge")
        print(f"  bands from Moriasi et al. (2007) Table 4: very good <{VOLUME_VERY_GOOD:.0f}%,"
              f" good <{VOLUME_GOOD:.0f}%, satisfactory <{VOLUME_SATISFACTORY:.0f}%\n")
        v = m["vol_verdict"].fillna(VOLUME_GREY).astype(int)
        for code in (VOLUME_BEST, VOLUME_GOOD_N, VOLUME_OK, VOLUME_BAD, VOLUME_GREY):
            n = int((v == code).sum())
            print(f"    {VOLUME_LABELS[code]:42s} {n:6d}  {100*n/len(v):5.1f}%")
        pb = m.loc[v != VOLUME_GREY, "vol_pbias"].dropna()
        if not pb.empty:
            print(f"\n  PBIAS where scored: median {pb.median():+.1f}%, "
                  f"median absolute {pb.abs().median():.1f}%")

    if "fl_verdict" in m.columns:
        print(f"\n{'='*62}\nDoes the model show a flood when the river floods?"
              f"\n{'='*62}")
        print(f"  floods above each series' own {FLOOD_RP}-year level, matched within "
              f"+/-{FLOOD_WINDOW} days, {FLOOD_SEP}-day declustering")
        print(f"  strong CSI >= {FLOOD_STRONG:.2f}, good >= {FLOOD_MIDDLE:.2f}, "
              f"poor = luck not ruled out at p <= {FLOOD_ALPHA:.2f}\n")
        f = m["fl_verdict"].fillna(FLOOD_GREY).astype(int)
        for code in (FLOOD_STRONG_N, FLOOD_GOOD, FLOOD_WEAK, FLOOD_POOR, FLOOD_GREY):
            n = int((f == code).sum())
            print(f"    {FLOOD_LABELS[code]:48s} {n:6d}  {100*n/len(f):5.1f}%")
        c = m.loc[f != FLOOD_GREY, "fl_csi"].dropna()
        if not c.empty:
            print(f"\n  CSI where scored: median {c.median():.3f}, "
                  f"p25 {c.quantile(.25):.3f}, p75 {c.quantile(.75):.3f}")
        if "fl_n_obs" in m.columns:
            k = m.loc[f != FLOOD_GREY, "fl_n_obs"].dropna()
            if not k.empty:
                print(f"  floods judged on: median {k.median():.0f}, "
                      f"min {k.min():.0f}, max {k.max():.0f}")

    if "tr_verdict" not in m.columns:
        return
    print(f"\n{'='*62}\nIs the river getting wetter or drier?  annual mean flow"
          f"\n{'='*62}")
    print(f"  Mann-Kendall p < {TREND_ALPHA}, years needing {TREND_MIN_DAYS} paired "
          f"days, {TREND_MIN_YEARS} years minimum\n")
    t = m["tr_verdict"].fillna(TREND_GREY).astype(int)
    for code in (TREND_AGREES, TREND_NONE, TREND_MISSES, TREND_INVENTS, TREND_GREY):
        n = int((t == code).sum())
        print(f"    {TREND_LABELS[code]:42s} {n:6d}  {100*n/len(t):5.1f}%")
    # The number that matters: "neither finds a trend" is not the model being
    # right, so the honest score is conditional on there being something to find.
    real = m[(m.get("tr_obs").notna()) & (m["tr_obs"] != 0)] if "tr_obs" in m else m.iloc[:0]
    if len(real):
        hit = int((real["tr_sim"] == real["tr_obs"]).sum())
        print(f"\n  where the gauge shows a real trend ({len(real)} gauges), "
              f"the model agrees at {hit} of them ({100*hit/len(real):.1f}%)")


def summarize(m: pd.DataFrame) -> None:
    col = KGE_COL
    v = m[col].dropna()
    print(f"\n{'='*62}\n{col} summary over {len(v)} gauges\n{'='*62}")
    # Percentiles only. The mean used to be printed here with a note not to
    # report it, which mostly ensured it got reported: KGE' is unbounded below,
    # so a single badly-scored gauge drags it past every percentile shown -- on
    # VPU 714 the mean lands at -0.338 against a median of +0.267. p50 is the
    # summary; a number nobody should quote does not need printing.
    for q in (0.05, 0.25, 0.50, 0.75, 0.95):
        print(f"  p{int(q*100):02d}  {v.quantile(q):8.3f}")
    print(f"\n  >= 0.75        {100*(v>=0.75).mean():5.1f}%")
    print(f"  >= 0.50        {100*(v>=0.50).mean():5.1f}%")
    print(f"  >= 0.00        {100*(v>=0.00).mean():5.1f}%")
    print(f"  >= {KGE_NO_SKILL} (skill) {100*(v>=KGE_NO_SKILL).mean():5.1f}%")

    yrs = m.loc[v.index, "n_pairs"] / 365.25
    print(f"\n  paired record per gauge (years): median {yrs.median():.1f}, "
          f"p05 {yrs.quantile(0.05):.1f}, p95 {yrs.quantile(0.95):.1f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vpu", type=int, default=714)
    ap.add_argument("--mode", choices=("both", "statistic", "decision"),
                    default="both",
                    help="both (default): write the metric table AND the "
                         "decision verdicts from one run, over one window, so "
                         "they cannot drift apart. statistic: the metric table "
                         "only (KGE', NSE, contingency, monthly). decision: "
                         "the verdicts only, which must be scored over the same "
                         "window as the metrics they will be shown beside.")
    ap.add_argument("--start", default=DATE_START)
    ap.add_argument("--end", default=DATE_END)
    ap.add_argument("--min-years", type=float, default=1.0,
                    help="minimum overlap between model and gauge")
    ap.add_argument("--bias-correct", action="store_true",
                    help="also score the VOLUME decision, which needs the "
                         "GEOGLOWS global bias correction. This is the only "
                         "part of a decision run that uses the network: one "
                         "request per reach, about 9 minutes for a full VPU at "
                         "12 workers. Off by default so a decision run stays "
                         "offline.")
    ap.add_argument("--refresh", action="store_true",
                    help="re-read the zarr instead of using the local cache")
    ap.add_argument("--outdir", default=OUTPUT_DIR)
    add_gauge_source_args(ap)
    ap.add_argument("--label", default=RUN_LABEL,
                    help="name for this run, shown on the web page")
    ap.add_argument("--warmup-years", type=int, default=0,
                    help="drop this many years from the START of the model "
                         "record before scoring, so spin-up from the assumed "
                         "initial state is not evaluated")
    ap.add_argument("--model-parquet", default=None,
                    help="score this parquet of modelled discharge instead of "
                         "the retrospective zarr: datetime index, one column "
                         "per reach id. Sub-daily input is averaged to daily "
                         "mean. Set --label to name it.")
    args = ap.parse_args()

    # Resolved first: everything below either hits the network or takes minutes,
    # and neither a wrong --data-dir nor a missing credential should cost that.
    # Constructing the source is itself the check -- the local backend stats its
    # inputs, the S3 one lists the bucket.
    source = source_from_args(args)
    note_unused_data_dir(args, source)

    if args.start is None or args.end is None:
        # Resolve the default window from whichever source is actually being
        # scored, so --model-parquet neither reaches the network nor inherits
        # the retrospective's dates.
        if args.model_parquet:
            first, last = parquet_period(args.model_parquet)
            src = os.path.basename(args.model_parquet)
        else:
            first, last = model_period()
            src = "the retrospective zarr"
        args.start = args.start or first
        args.end = args.end or last
        print(f"window not given; using the full record of {src} "
              f"{args.start} .. {args.end}")

    os.makedirs(args.outdir, exist_ok=True)

    gauges = build_gauge_table(args.vpu, source)
    if args.model_parquet:
        model = model_q_from_parquet(args.model_parquet,
                                     gauges["final_river_id"].to_numpy(),
                                     args.start, args.end, args.vpu, args.refresh)
    else:
        model = fetch_model_q(gauges["final_river_id"].to_numpy(), args.start,
                              args.end, args.vpu, args.refresh)

    # Warm-up trim. A routing model starts from an assumed state, so its first
    # months of output partly reflect that state rather than the forcing.
    # Dropping the first --warmup-years removes them from the scoring.
    #
    # The trim happens HERE, after the fetch, not by moving --start forward. The
    # cache is keyed on the requested window, so moving --start would name a
    # cache that does not exist and re-read the source for nothing -- and every
    # change to --warmup-years alone would pay for a fresh read.
    #
    # So args.start names the FETCH window while the array is being fetched, is
    # saved into cache_start, and is then overwritten below with the evaluation
    # start -- which is what run.json records as date_start and what labels the
    # run everywhere downstream.
    cache_start = args.start
    if args.warmup_years < 0:
        sys.exit(f"--warmup-years must be 0 or more, got {args.warmup_years}")
    if args.warmup_years:
        cut = model.index.min() + pd.DateOffset(years=args.warmup_years)
        kept = model.index >= cut
        if not kept.any():
            sys.exit(f"--warmup-years {args.warmup_years} removes the whole record "
                     f"({model.index.min().date()} .. {model.index.max().date()})")
        model = model.loc[kept]
        args.start = model.index.min().strftime("%Y-%m-%d")
        print(f"      warm-up: dropped the first {args.warmup_years}y, "
              f"scoring {args.start} .. {args.end} ({len(model):,} days)")

    corrections = None
    if args.bias_correct and args.mode in ("decision", "both"):
        corrections = fetch_corrections(gauges["final_river_id"], model)
    elif args.bias_correct:
        print("      --bias-correct ignored: it only feeds the volume decision")

    metrics = compute_metrics(gauges, model, args.min_years, source, args.mode,
                              corrections)

    # Gauge attributes belong in both tables; everything named hs_* or tr_* is
    # a decision and belongs only in the decision one.
    ATTRS = ("final_river_id", "gauge_id", "fname", "latitude", "longitude",
             "strmOrder", "USContArea", "koppen", "ISO_A3", "river_name",
             "first_day", "last_day", "n_pairs")
    is_decision = lambda c: c.startswith(("hs_", "tr_"))

    written = []
    if args.mode in ("statistic", "both"):
        stat = metrics[[c for c in metrics.columns if not is_decision(c)]]
        pq = os.path.join(args.outdir, f"vpu{args.vpu}_metrics.parquet")
        print(f"[4/5] writing {len(stat)} rows -> {pq}")
        stat.to_parquet(pq, index=False)
        written.append(pq)
    if args.mode in ("decision", "both"):
        cols = [c for c in metrics.columns if is_decision(c) or c in ATTRS]
        dec = metrics[cols]
        # Only gauges that answered at least one decision reach this table; the
        # rest still appear in the metric table, and serve.py greys them.
        vcols = [c for c in ("hs_verdict", "tr_verdict") if c in dec.columns]
        if vcols:
            dec = dec[dec[vcols].notna().any(axis=1)]
        pq = os.path.join(args.outdir, f"vpu{args.vpu}_decisions.parquet")
        print(f"      writing {len(dec)} rows -> {pq}")
        dec.to_parquet(pq, index=False)
        written.append(pq)
    pq = written[0]

    # Record the run configuration next to the parquet. serve.py and
    # build_webapp.py read this rather than importing the module constants, so
    # that a non-default --start/--end cannot leave them describing, or slicing
    # to, a window the metrics were not computed over.
    # Under "both" the SAME config is written beside both parquets. That is the
    # point of the combined mode: one resolved window, recorded identically in
    # two places, so serve.py's window check cannot fail on files that came out
    # of one run. They previously drifted whenever the two modes were run on
    # different days, because an unset --end resolves to the zarr's end date,
    # and the retrospective advances.
    cfgs = []
    if args.mode in ("statistic", "both"):
        cfgs.append(os.path.join(args.outdir, f"vpu{args.vpu}_run.json"))
    if args.mode in ("decision", "both"):
        cfgs.append(os.path.join(args.outdir, f"vpu{args.vpu}_decisions_run.json"))
    for cfg in cfgs:
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({
                "vpu": args.vpu,
                "mode": args.mode,
                "label": args.label,
            # date_start is the EVALUATION start, after any warm-up trim, and is
            # what the page reports. cache_start is the window the model array
            # was fetched over and names the cache file; build_webapp.py needs
            # it to find the same array. They differ only when --warmup-years
            # is used.
                "date_start": args.start,
                "date_end": args.end,
                "cache_start": cache_start,
                "warmup_years": args.warmup_years,
                "min_years": args.min_years,
                "n_gauges": int(len(metrics)),
                # Which observations were scored. A local directory carries no
                # version of its own, so without this the same command can
                # produce different numbers from a re-downloaded copy with
                # nothing saying so; for S3 the snapshot tag pins it exactly.
                "gauge_source": source.kind,
                "gauge_source_detail": source.describe(),
                "kge_col": KGE_COL,
                "kge_label": KGE_LABEL,
                "kge_no_skill": KGE_NO_SKILL,
            }, fh, indent=2)
        print(f"      run config -> {cfg}")

    if args.mode in ("decision", "both"):
        # No decision map is drawn here: the PNG is the KGE' map, and the
        # decision verdicts are a map serve.py draws, not this script.
        summarize_decision(metrics)
    if args.mode == "decision":
        print(f"\nwrote {pq}")
        return

    png = os.path.join(args.outdir, f"vpu{args.vpu}_kge_map.png")
    plot_kge_map(metrics, args.vpu, png, args.start, args.end, args.label)

    summarize(metrics)
    written.append(png)
    print("\n" + "\n".join(f"wrote {p}" for p in written))


if __name__ == "__main__":
    main()
