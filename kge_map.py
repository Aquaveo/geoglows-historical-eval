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

# Where the observed-gauge inputs live. There is no useful default -- the gauge
# CSVs are not part of this repository -- so set GEOGLOWS_EVAL_DATA, or pass
# --data-dir to this script. "data" is a placeholder that makes the failure
# legible rather than a path that happens to work on one machine.
#
# The directory must contain:
#     master_catalog_with_metadata.xlsx     the gauge catalog
#     routing/gauge_data/*.csv              one CSV per gauge
DATA_DIR = os.environ.get("GEOGLOWS_EVAL_DATA", "data")


def data_paths(data_dir: str) -> tuple[str, str]:
    """Resolve the catalog and gauge-CSV locations under `data_dir`.

    Fails immediately and by name if either is missing. Both are read much later
    -- the catalog in build_gauge_table(), the CSVs one at a time during pairing
    -- so without this a wrong --data-dir surfaces either as a pandas error on an
    unrelated line or, worse, as every gauge being silently skipped for "no CSV
    on disk", which reads as missing data rather than a wrong path.
    """
    catalog = os.path.join(data_dir, "master_catalog_with_metadata.xlsx")
    gauges = os.path.join(data_dir, "routing", "gauge_data")
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
    """Gauges from a directory produced by download_observed_data.py."""

    kind = "local"

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.catalog_path, self.gauge_dir = data_paths(data_dir)

    def describe(self) -> str:
        return f"local:{os.path.abspath(self.data_dir)}"

    def catalog(self) -> pd.DataFrame:
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
                 date_tag: str = GAUGE_DATE_TAG):
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

    What selects local is a gauge directory actually being there, not --data-dir
    being passed: an S3 run reads nothing local, so the flag has no effect on one.
    """
    d = data_dir or DATA_DIR
    if kind == "local":
        return LocalGaugeSource(d)
    if kind == "s3":
        return S3GaugeSource(profile=profile, date_tag=date_tag)

    if os.path.isdir(os.path.join(d, "routing", "gauge_data")):
        return LocalGaugeSource(d)

    # --aws-profile names a profile for THIS bucket and nothing else, so passing
    # it is an explicit request for S3 -- but only once local has been ruled out,
    # so a profile left in the environment cannot override a local copy.
    if profile:
        return S3GaugeSource(profile=profile, date_tag=date_tag)

    raise SystemExit(
        "no local gauge data, and nothing asked for S3.\n"
        f"  looked for: {os.path.join(d, 'routing', 'gauge_data')}\n"
        "\n"
        "  Preferred -- use a local copy:\n"
        "    point --data-dir (or $GEOGLOWS_EVAL_DATA) at a directory holding\n"
        "    routing/gauge_data/*.csv\n"
        "\n"
        "  Or read from the private bucket, which needs credentials that reach it:\n"
        f"    --gauge-source s3   (bucket: {GAUGE_BUCKET})\n")


def add_gauge_source_args(ap: argparse.ArgumentParser) -> None:
    """The gauge-source flags, identical across all three scripts."""
    ap.add_argument("--data-dir", default=DATA_DIR,
                    help="directory holding routing/gauge_data/ and, for local "
                         "runs, master_catalog_with_metadata.xlsx. Defaults to "
                         "$GEOGLOWS_EVAL_DATA. THE PREFERRED WAY to supply "
                         "gauges.")
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
    return gauge_source(args.data_dir, args.gauge_source,
                        args.aws_profile, args.gauge_date_tag)


def note_unused_data_dir(args, source) -> None:
    """Say so when --data-dir was passed but cannot affect this run.

    An S3 source reads nothing local, so --data-dir cannot affect the run while
    still looking like it might. Called from every script, since this is now
    true of all three.
    """
    if source.kind == "s3" and args.data_dir != DATA_DIR:
        print(f"note: --data-dir {args.data_dir} is not used when reading gauges "
              f"from S3.\n      It applies to --gauge-source local.")


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


def compute_metrics(gauges: pd.DataFrame, model: pd.DataFrame,
                    min_years: float, source=None) -> pd.DataFrame:
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

        rows, n_stage, n_short, n_empty = [], 0, 0, 0
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

            st = pair_stats(both["sim"].to_numpy("float64"),
                            both["obs"].to_numpy("float64"))
            st.update(skill_scores(both))
            # Thresholds from each series' whole record in the window; the
            # contingency counts below still use the paired days only.
            # sim is already indexed on model.index; only obs needs clipping to it.
            st.update(contingency_stats(both, sim.dropna(),
                                        obs.reindex(model.index).dropna()))
            st.update(monthly_stats(both))
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
          f"model array")

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
    ap.add_argument("--start", default=DATE_START)
    ap.add_argument("--end", default=DATE_END)
    ap.add_argument("--min-years", type=float, default=1.0,
                    help="minimum overlap between model and gauge")
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

    metrics = compute_metrics(gauges, model, args.min_years, source)

    pq = os.path.join(args.outdir, f"vpu{args.vpu}_metrics.parquet")
    print(f"[4/5] writing {len(metrics)} rows -> {pq}")
    metrics.to_parquet(pq, index=False)

    # Record the run configuration next to the parquet. serve.py and
    # build_webapp.py read this rather than importing the module constants, so
    # that a non-default --start/--end cannot leave them describing, or slicing
    # to, a window the metrics were not computed over.
    cfg = os.path.join(args.outdir, f"vpu{args.vpu}_run.json")
    with open(cfg, "w", encoding="utf-8") as fh:
        json.dump({
            "vpu": args.vpu,
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
            # version of its own, so without this the same command can produce
            # different numbers from a re-downloaded copy with nothing saying
            # so; for S3 the snapshot tag pins it exactly.
            "gauge_source": source.kind,
            "gauge_source_detail": source.describe(),
            "kge_col": KGE_COL,
            "kge_label": KGE_LABEL,
            "kge_no_skill": KGE_NO_SKILL,
        }, fh, indent=2)
    print(f"      run config -> {cfg}")

    png = os.path.join(args.outdir, f"vpu{args.vpu}_kge_map.png")
    plot_kge_map(metrics, args.vpu, png, args.start, args.end, args.label)

    summarize(metrics)
    print(f"\nwrote {pq}\nwrote {png}")


if __name__ == "__main__":
    main()
