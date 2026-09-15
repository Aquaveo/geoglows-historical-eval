# What this app does

Reference for the GEOGLOWS RFS retrospective evaluation. Written 2026-09-03, rewritten
2026-09-07.

This is the single source of truth for how the pieces fit together. The per-metric
definitions live in the module docstring of [`kge_map.py`](kge_map.py) and are **not**
duplicated here — two copies of the same documentation is how the two frontends drifted
apart, and one of them silently lost a metric row for several days.

---

## 1. The question it answers

**Is a model's daily discharge any good, and where is it worst?**

It compares modelled daily discharge against observed stream-gauge discharge, one gauge at
a time, and presents the result three ways: a map, a per-gauge inspector, and grouped
summaries.

Currently scoped to **VPU 714** — the Missouri/Mississippi basin, 223,906 river reaches, of
which 2,632 carry a gauge CSV and **2,626 survive the filters** on the full retrospective.
This is a demo scope, deliberately small. See [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) for what
that scoping hides.

The longer-term question is *"is the model improving between versions?"*. The app now
supports scoring **any** discharge source and labelling the run, so several versions can be
built as separate pages and compared side by side. What still does not exist is the
machinery that would answer the question properly: paired per-gauge differences, change
maps, improved-vs-degraded counts. Comparing two pages by eye is not that.

---

## 2. The data sources, and how they are joined

**Model side — two ways in.**

| source | how | when |
|---|---|---|
| GEOGLOWS retrospective zarr | read straight off S3 | default |
| a wide parquet of discharge | `--model-parquet <path>` | scoring routed or other model output |

Parquet is the format `river-route` already writes (`Muskingum.py` → `to_parquet`) and the
one RAT overwhelmingly reads, so for this project's own model output the second row is the
route. A source in some other format should be converted to parquet — one line of pandas —
rather than fed in any other way.

Both paths write the same array: `cache/model_q_vpu<VPU>_<start>_<end>.npz`, holding `q`
(time × river, float32), `dates`, `river_ids`, and `source`. **That npz is an internal
cache, not an input format.** It is what `build_webapp.py` and `serve.py` read, which is why
a routed hourly parquet could be scored before `--model-parquet` existed — by writing the
cache by hand. That is no longer necessary and is no longer supported: a cache without a
`source` stamp is rejected and rebuilt.

`source` records what built the cache (the zarr URL, or the parquet's path, size and mtime).
Without it the cache is keyed on vpu and window alone, which cannot tell two sources covering
the same dates apart — so pointing `--model-parquet` at a new file would silently reuse the
previous file's numbers under the new `--label`. Caches written before stamping existed are
therefore refused; the first run after this change pays one re-read per window.

The zarr:

```
http://geoglows-v2.s3-us-west-2.amazonaws.com/retrospective/daily.zarr
```

Daily mean discharge, m³/s. Shape `(31656 time, 6838900 river_id)` covering 1940-01-01 to
2026-09-01 as of this writing — it grows, which is why nothing hard-codes the length. The
time axis is *seconds since 1940-01-01*; that epoch **is** hard-coded, and a zarr declaring
a different one would silently produce wrong dates.

The chunking matters more than anything else about this array: **`(31047, 50)`** — each
chunk holds the **entire time series for 50 rivers**. So pulling one river costs the same
as pulling fifty, and the efficient access pattern is to group the reaches you want by
chunk and read one chunk-aligned column block at a time. That is what `fetch_model_q()`
does, on 16 threads. 1,487 chunks for VPU 714, about 85 seconds.

`--model-parquet` expects a datetime index and one column per reach id. It reads **only the
columns that carry a gauge** — parquet is columnar, so the rest are never touched. Reads
scale with column count: 100 columns 0.4 s / 92 MB, 2,631 columns 5.5 s / 2,380 MB.
Sub-daily input is averaged to **daily mean**, matching how the daily zarr is built from
hourly routing. That is not neutral: on a 200-reach sample the daily *maximum* ran 39%
above the daily mean at the median reach.

**Observed side.** Per-gauge CSVs, schema `datetime,discharge`, from either of two places:

```
s3://master-gauge-data/production/production-{ISO_A3}-{provider}-{YYYYMMDD}/measurements/{station}.csv
<data-dir>/routing/gauge_data/{ISO_A3}_{provider}_{station}.csv
```

**A local directory is the default**, used whenever `<data-dir>/routing/gauge_data/` exists,
with metadata from `<data-dir>/master_catalog_with_metadata.xlsx` (37,528 rows). `<data-dir>`
comes from `--data-dir` or `$GEOGLOWS_EVAL_DATA`. It is preferred on three counts: ~15 ms a
gauge against 20 ms threaded, no credentials, and it works offline.

The bucket needs no local input at all — it publishes its own catalog, one `catalog.csv` per
provider, 37,529 rows concatenated — but it is **private, and opt-in**. Reaching it requires
`--gauge-source s3` or an `--aws-profile`; with neither, and no local directory, the run stops
and says so rather than silently reaching for a bucket most readers of this repository cannot
access. `--gauge-source` forces either backend.

The source is resolved before any other work, so a wrong path or an unusable credential fails
immediately rather than surfacing as zero gauges found.

The two backends live behind one interface in `kge_map.py` — `catalog()`, `resolve()`,
`locate()`, `open()` — and nothing downstream knows which is in use. The flattened
`{ISO_A3}_{provider}_{station}.csv` spelling is the gauge's **identity** either way: it is
what reaches the metrics parquet, so a parquet scored from one backend can be rebuilt into a
page by the other. `provider_for()` reconstructs the provider from the xlsx's free-text
columns for the local path only; the bucket takes it from the prefix name, where it is
authoritative rather than inferred.

The bucket catalog carries no Köppen group, so an S3 run leaves that column empty and the
page's Köppen grouping with it; a local run still gets it from the xlsx. The xlsx also
matches 840 gauges the bucket marks `-1`, and those are **deliberately not used** — they are
CARAVAN republications of stations the bucket already matches natively. See KNOWN_ISSUES
section S.

All sampled series are daily — 0 of 400 sampled files carry sub-daily rows — so no resampling
happens. Files that do would be handled by keeping the **first** reading of each day, not by
averaging.

**The join.** The catalog column `final_river_id` is a TDX-Hydro `LINKNO`, which is the
model's `river_id`. Stream order and drainage area come from a third source:

```
http://geoglows-v2.s3-us-west-2.amazonaws.com/tables/v2-model-table.parquet
```

`LINKNO → strmOrder, USContArea (m²), VPUCode`.

**Paired days only.** Every metric is computed on an inner join of the two series with NaNs
dropped — days where *that gauge* and the model both have a value. Consequence worth
internalising: **every gauge has a different sample**. On the full retrospective the median
paired record is 52 years; on a 25-year routed window it is 21. Anything aggregated across
gauges is aggregating across unequal samples.

---

## 3. The pipeline

Three scripts, run in this order. Only the first computes anything.

```
kge_map.py  ──►  <outdir>/vpu714_metrics.parquet  ──►  serve.py        (live app)
                 <outdir>/vpu714_run.json          └─►  build_webapp.py (static page)
                 <outdir>/vpu714_kge_map.png
                 cache/model_q_vpu714_<start>_<end>.npz
```

### `kge_map.py` — the only place a metric is calculated

1. `build_gauge_table()` — catalog filtered to `final_river_id > 0` (a `-1` sentinel affects
   31% of the global catalog), joined to the network table, restricted to the VPU, checked
   for a CSV on disk.
2. The model array — from the cache if present, else the zarr or `--model-parquet`.
3. Optional warm-up trim (`--warmup-years`).
4. `compute_metrics()` — per gauge: load the CSV, inner-join, then `pair_stats()` +
   `skill_scores()` + `contingency_stats()` + `monthly_stats()`.
5. Deduplicates gauges sharing a reach, keeping the longest paired record (1 gauge here).
6. Writes the parquet (**2,626 rows × 284 columns** on the full run) and the static PNG.

Filters, and what they cost on the full VPU 714 record:

| Filter | Effect |
|---|---|
| `final_river_id > 0` | drops unmatched sentinels |
| CSV must exist on disk | 2,632 of 2,665 catalog gauges |
| value column must be `discharge` | drops 5 stage-only files |
| `--min-years` (default **1**) | drops 0 here; at the old default of 10 it dropped 41 |

`--min-years` counts **paired days**, not elapsed years — `round(min_years × 365.25)`. A
gauge reporting sparsely for thirty years can still fail it.

The default was lowered from 10 to 1 to favour coverage. It is nearly inert on an 86-year
record but decisive on a short one: over 2000–2024 the 10-year rule dropped **657** gauges.
A one-year record gives an unstable KGE′ — median 0.175 away from that gauge's full-record
value, and 45.4% of gauges that clear −0.41 overall fail it in at least one single year.
`KNOWN_ISSUES.md` §O has the full measurement and the two knock-on effects (no flood scores
under 10 annual maxima; no monthly metrics under 60 paired days in a month).

**The default window is the full record of whatever is being scored** — the zarr's time
axis, or the parquet's index — resolved at run time rather than hard-coded. Pass
`--start`/`--end` to restrict it.

With `--warmup-years N` the fetch window and the evaluation window differ. The trim happens
**after** the fetch, because the cache is keyed on the requested window: moving `--start`
would name a cache that does not exist and re-read the source for nothing. `run.json`
therefore records both — `cache_start` names the cache file, `date_start` is where scoring
began.

`--label` names the run. It appears in the PNG footer, the page, and the browser tab. Set it
whenever the scored discharge is not the default retrospective — otherwise two runs produce
outputs identical apart from their numbers, and both claim to be GEOGLOWS v2.

### `serve.py` — the live local app

Stdlib `http.server`, no Flask, **no network access**. Serves `webapp/explorer.html` and:

- **`/api/gauges`** — the whole metric table plus basemap outlines, built once at startup.
- **`/api/series?id=<river_id>`** — that reach's daily series sliced out of the model array
  held in memory, plus the observed CSV and a smoothed observed day-of-year climatology.

The model array is the same `.npz` the metrics came from, loaded once (~100–350 MB float32)
and **clipped to the evaluation window at load time**. It previously called
`geoglows.data.retro_daily()` per reach, which was harmless only while the scored model and
the published retrospective were the same numbers — once a run scores routed output, that
drew a GEOGLOWS hydrograph beside metrics computed from something else.

Because the array is clipped at load, a warm-up year excluded from the metrics is drawn
nowhere either. Charts and numbers describe the same days by construction.

### `build_webapp.py` — the shareable static page

Precomputes a flow-duration curve and monthly regime per gauge, adds the basemap, and
substitutes the whole payload into `webapp/explorer.html`, producing one self-contained HTML
file (**5.8–7.6 MB** for the current runs) with no server, no CDN and no tile provider.

It loads the cache by `cache_start` and trims to `date_start`, so its curves use exactly the
days the metrics used.

---

## 4. One source file, two deployments

`webapp/explorer.html` (1,908 lines) is the only frontend file. It previously existed as two
copies that drifted; they were 71% identical and one lost a metric row.

The page detects its own mode at boot:

```js
const EMBEDDED = /* parse #payload, or null if the placeholder is untouched */;
const LIVE = !EMBEDDED;
```

`build_webapp.py` substitutes the payload placeholder; `serve.py` leaves it alone, and an
unsubstituted placeholder means *run live*. `build_webapp.py` asserts the placeholder
appears **exactly once** — writing that token inside a code comment once caused the payload
to be injected twice.

Only the per-gauge detail differs between modes:

| | Live (`serve.py`) | Static |
|---|---|---|
| Gauge table | fetched from `/api/gauges` | embedded |
| Daily hydrograph | yes, sliced per click | **no** — too large to embed |
| FDC + monthly regime | derived from the daily series | precomputed at build time |
| Shareable file | no | yes |

Both are computed on the **same sample**: the evaluation window recorded in `run.json`. In
live mode every returned day is now inside that window, so `win` spans the whole series.
Verified when the two were computed differently: live and static flow-duration curves agreed
to 0.02%, which is the payload's 4-significant-figure rounding.

Everything else — map, colours, legend, filters, summary tab, tables, rank — is shared code.

---

## 5. The interface

### Map tab

A **canvas** map (not SVG, and not a tile map — the artifact sandbox blocks tile servers, so
coastlines and state borders are simplified polylines embedded in the payload).
Equirectangular projection with a standard parallel at the data's mid-latitude.

- **Colour by** — 16 metrics. Diverging red↔grey↔blue for metrics with a meaningful centre;
  a sequential red ramp on a log scale for error magnitudes, which have no centre.
- **Filters** — minimum record length, stream-order chips, below-no-skill only.
- Points are drawn **best-first so the worst land on top**. With thousands of overlapping
  markers the last drawn wins, and hiding poor gauges under good ones would flatter the
  model exactly where it matters.

### Gauge inspector (click a gauge)

Numbers first, then explanations, then charts:

1. The mapped metric as a headline, with a verdict pill **only** when that metric is KGE′ —
   the bands (0.5 / 0 / −0.41) are defined for KGE′ and mean nothing for r or PBIAS.
2. **Every metric**, not just the mapped one.
3. **Why KGE′ lands there** — r, γ, β as distance-from-optimum bars; the longest bar is the
   binding constraint.
4. **Rank among shown gauges** — a percentile sentence. Positions by *rank*, never by value,
   which is what makes it immune to the unbounded tails.
5. Daily discharge (live only) — observed / simulated / climatology, log or linear, zoomable.
6. Flow duration curve, mean monthly regime.
7. **Flood detection** — a 2×2 contingency matrix at each series' own 2-year return level,
   with POD / FAR / CSI / ETS / frequency bias derived beneath it.
8. **By month** — one row per calendar month across 11 selectable metrics.

### Summary tab

Headline tiles, then a breakdown by **Overall / Stream order / Köppen group / Month**, then a
percentile table for every metric.

Bars for stream order and Köppen; a polar chart for month, because month is the one grouping
that is genuinely cyclical — December sits next to January. The polar radial axis spans the
**data**, not the metric's full range: on a −1…1 axis a spread of 0.32–0.53 puts every wedge
at almost the same radius and hides the contrast the chart exists to show.

Adding a grouping is not quite a one-line change. `GROUPINGS` is a four-entry table, and the
generic bucketing path handles any categorical payload key — but `month` is special-cased in
five places, and a key missing from `GROUPINGS` throws before the tab renders. A gauge
attribute also has to be added in **two** places in `kge_map.py`: `build_gauge_table`'s
`keep` list *and* `compute_metrics`'s `st.update`. Adding it to only the first silently drops
it, which is exactly what happened to `river_name`.

---

## 6. Running it

```bash
python kge_map.py                    # ~2.5 min warm, writes parquet + run.json + PNG
python build_webapp.py               # the shareable single-file page
python serve.py                      # then open http://localhost:8765
```

Scoring a different model, into its own directory so the baseline survives:

```bash
python kge_map.py --vpu 714 --model-parquet routed.parquet \
    --label "routing v7" --warmup-years 1 --outdir outputs_v7
python build_webapp.py --vpu 714 --metrics outputs_v7/vpu714_metrics.parquet \
    --out outputs_v7/vpu714_explorer.html
```

Two runs side by side — two ports, or two static files:

```bash
python serve.py --metrics outputs/vpu714_metrics.parquet     --port 8765
python serve.py --metrics outputs_v7/vpu714_metrics.parquet  --port 8766
```

The static pages need no server at all; opening the file works. For stable URLs,
`python -m http.server 8080 --directory .` serves every run's directory at once.

From WSL, `explorer.exe "$(wslpath -w outputs/vpu714_explorer.html)"` opens a Windows
browser.

Environment: [`environment.yml`](environment.yml). Two traps baked into it — **zarr is
pinned `<3`** because the 3.x rewrite changed the store API this code uses, and cartopy is
installed in `~/.local` on this machine, which leaks into every Python 3.12 env. Verify a
clean env with `PYTHONNOUSERSITE=1 python serve.py`.

If `serve.py` seems to ignore your changes, an orphaned process is probably still holding
the port: `pgrep -af "[s]erve\.py"`.

---

## 7. Division of labour: what is Python, what is JavaScript

**Python computes every metric.** Anything you would quote in a report comes from
`kge_map.py` and lands in the parquet, which is the auditable artifact — open it in pandas
and you can reproduce any number in the interface.

**JavaScript only aggregates and draws.** Three things exist only in the browser:

- the grouped medians and percentiles on the Summary tab, computed from the payload so
  changing grouping is instant;
- the rank percentile, which depends on the current filter;
- the FDC and monthly regime in live mode, derived from the daily arrays.

None of that is persisted. The rule: **view-dependent numbers are computed in the browser
and not stored; everything else is Python and is stored.**

---

## 8. What it deliberately does not do

- **No probabilistic scores.** The model is deterministic and will stay so, so RPS, CRPS,
  Brier, reliability diagrams, rank histograms and ROC/AUC are permanently out of scope.
  CRPS of a point forecast equals MAE exactly.
- **Only KGE′**, not KGE 2009. Note the RAT evaluation scripts compute *both*; on the same
  gauges and days the two differ by a median of 0.115, and by more than 0.05 at 85% of
  gauges.
- **No bias-correction comparison.** SABER and locally-applied correction are out of scope
  for now.
- **No paired version comparison** — several versions can be *built and viewed*, but nothing
  computes per-gauge differences between them.
- **No "broken gauge" screen.** RAT drops gauges with `alpha < 0.1` or `|beta − 1| > 0.9`;
  this does not. On one routed run that screen would have removed 12% of gauges and moved
  median KGE′ from +0.262 to +0.292, and 16% of what it removes scores above the no-skill
  line.
- **No log-space or relative metrics** (log-NSE, MAPE, MALE, 1/Q) — all need an explicit
  zero-flow policy that has not been decided.
- **No donor-gauge benchmark.** Declined. This matters for interpretation: `ss_clim` and NSE
  are both scored against a reference built from the target gauge's own record, so neither
  can be computed at an ungauged reach and neither supports the claim *"the model adds value
  where we deploy it"*. See `KNOWN_ISSUES.md` §H.

---

## 9. File map

| File | Role |
|---|---|
| `kge_map.py` | computes every metric; writes the parquet, run.json and the PNG |
| `serve.py` | local server; slices the model array per reach; no network |
| `build_webapp.py` | inlines the payload into the single-file static page |
| `webapp/explorer.html` | the only frontend; both deployments |
| `environment.yml` | conda environment |
| `KNOWN_ISSUES.md` | data-quality problems and deliberately-parked decisions |
| `<outdir>/vpu714_metrics.parquet` | **the auditable artifact** — one row per gauge |
| `<outdir>/vpu714_run.json` | label, windows and filters; read by both consumers |
| `<outdir>/vpu714_kge_map.png` | static map, footer names the run |
| `<outdir>/vpu714_explorer.html` | built static page |
| `cache/model_q_vpu*.npz` | the model array — the seam every source converges on |
| `cache/basemap_vpu*.json` | simplified outlines; disposable |

Runs built so far, each in its own directory:

| directory | label | window | filters | gauges |
|---|---|---|---|---|
| `outputs/` | GEOGLOWS v2 retrospective | 1940–2026 | min 1y | 2,626 |
| `outputs_routed_v7/` | Routed v7 | 2000–2024 | min 10y | 1,970 |
| `outputs_routed_v7_warm1/` | Routed v7 — 1y warm-up trim | 2001–2024 | min 10y, 1y trim | 1,951 |
| `outputs_routed_v7_ratmatch/` | Routed v7 — RAT filters | 2001–2024 | min 1y, 1y trim | 2,202 |

These are **not** directly comparable — different windows, different gauge sets.

---

## 10. Three things that will mislead you

Repeated from `kge_map.py` because they are the errors most likely to be made when reading
the output. Numbers measured on the current full VPU 714 run.

1. **KGE′, NSE, `ss_clim` and the ratios are unbounded below.** Worst values here are −151,
   −10,630 and −8,280. Never take a mean across gauges; the median is used everywhere for
   this reason.
2. **`rmse` and `mae` are not comparable between gauges.** RMSE correlates with drainage
   area at Spearman **+0.76**, so a map of it is largely a map of river size. The normalised
   versions fix that but inherit a variability confound instead (**+0.84** with the observed
   CV), so flashy and arid gauges look worse at equal model quality.
3. **Spearman ρ is invariant to any monotone transform**, so a per-reach quantile mapping or
   SABER-style correction cannot change it. Good diagnostic, useless as a verdict on a bias
   correction — a null result there is expected, not a finding.
