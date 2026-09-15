# geoglows-historical-eval

Scores modelled daily river discharge against observed stream gauges, one gauge at a time,
and publishes the result as an interactive map, a per-gauge inspector and grouped summaries.

Built to answer *"is this model any good, and where is it worst?"* for the GEOGLOWS v2
retrospective, and to compare it against alternative model runs — routed output, parameter
variants, anything that can be written as a parquet of discharge.

Currently scoped to **VPU 714** (Missouri/Mississippi) as a demo.

## What you need before running

**Gauge observations** — one CSV per gauge, columns `datetime,discharge`, named
`{ISO_A3}_{provider}_{station}.csv`.

**Use a local copy. This is the recommended way and the default.** Put the CSVs under
`routing/gauge_data/` and point `--data-dir` at the directory above it, or set
`$GEOGLOWS_EVAL_DATA` once:

```bash
echo 'export GEOGLOWS_EVAL_DATA=/path/to/your/gauge/data' >> ~/.bashrc && source ~/.bashrc
```

Local is faster — about 15 ms a gauge against 20 ms threaded and 148 ms unthreaded — needs no
credentials, and works offline. Nothing else has to be configured: if
`<data-dir>/routing/gauge_data/` exists, that is what gets read.

You also need `master_catalog_with_metadata.xlsx` in the same directory for a local run. It
supplies `final_river_id`, `gauge_id`, `ISO_A3`, `latitude`, `longitude`, and the Köppen group
the page groups by.

### Reading from S3 instead

The gauges are also published to a **private** bucket, `master-gauge-data`. Reading from it is
**opt-in and never happens by default** — if there is no local data and you have not asked for
S3, the run stops and tells you so rather than reaching for the network.

Most people cannot reach this bucket. You need AWS credentials carrying `s3:ListBucket` on it
and `s3:GetObject` on `production/*`. Check yours before running anything:

```bash
aws s3 ls s3://master-gauge-data/production/ --profile your-profile
```

Then ask for it explicitly, either way round:

```bash
python kge_map.py --vpu 714 --gauge-source s3 --aws-profile your-profile
```

Credentials come from the usual chain — a `[default]` profile, `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`, an instance role, or `--aws-profile`. An S3 run reads nothing local,
so it needs no catalog and `--data-dir` has no effect on it; it also has no Köppen grouping,
since the bucket's catalog carries no such column.

Whichever ran is recorded in `vpu<VPU>_run.json`, with the bucket snapshot date for S3 runs,
so two sets of results are either comparable or provably not.

**The model** — nothing to prepare. The GEOGLOWS retrospective zarr is public HTTP with no
credentials, or pass `--model-parquet` to score your own run from a local source.

## Running it

```bash
conda env create -f environment.yml && conda activate geoglows-eval
```

Score the model. This always comes first, and writes the metrics parquet, the run config and a
static PNG map into `outputs/`:

```bash
python kge_map.py --vpu 714
```

Then look at the results — **start the server**:

```bash
python serve.py --vpu 714
```

Open <http://localhost:8765>. This is the way to use the tool day to day: it is the full
version, and the only one that draws each gauge's **daily hydrograph**, read live when you
click. It never writes a file, so re-running it costs nothing.

### Only when you need to send it to someone

`build_webapp.py` is **not a step in the pipeline** — it is a second, independent way of
viewing the same metrics parquet, and most of the time you will not need it:

```bash
python build_webapp.py --vpu 714
```

It bakes everything into one HTML file — data, charts, basemap, all inlined — that opens in
any browser with no Python, no server and no AWS access. That is the only thing it is for:
emailing a result, attaching it to a paper, archiving a run.

The trade is the hydrographs. ~2,600 daily series will not fit in a file, so the static page
carries the flow-duration curve and monthly regime instead. Everything else is identical, and
neither script needs the other to have run.

| | `serve.py` | `build_webapp.py` |
|---|---|---|
| Use it | **by default** | only to hand a result to someone |
| Output | a local server on :8765 | one portable HTML file |
| Daily hydrographs | yes | no |
| Viewer needs Python and the gauge data | yes | no |

## Comparing several model runs

Every model gets scored separately and viewed separately. Nothing is shared between runs
except the gauges they are scored against.

**Score each one into its own `--outdir`**, or the second overwrites the first:

```bash
python kge_map.py --vpu 714
```

```bash
python kge_map.py --vpu 714 --model-parquet routed.parquet --label "routing v7" --warmup-years 1 --outdir outputs_v7
```

**Then serve each on its own port** and put the two browser tabs side by side:

```bash
python serve.py --vpu 714 --port 8765
```

```bash
python serve.py --vpu 714 --metrics outputs_v7/vpu714_metrics.parquet --port 8766
```

`--label` names the run on its page, its browser tab and the map footer. **Set it on every
non-default run.** Without it the two tabs are identical apart from their numbers and both
claim to be GEOGLOWS v2 — which is exactly the confusion a comparison is meant to resolve.

If instead you want to *send* both runs to someone, build a file per run. `build_webapp.py`
has no `--outdir`, so point `--metrics` and `--out` into each directory yourself:

```bash
python build_webapp.py --vpu 714
```

```bash
python build_webapp.py --vpu 714 --metrics outputs_v7/vpu714_metrics.parquet --out outputs_v7/vpu714_explorer.html
```

## The files

| | |
|---|---|
| `kge_map.py` | **the only place a metric is calculated.** Its module docstring defines every one |
| `build_webapp.py` | inlines the payload into a single shareable HTML file |
| `serve.py` | local server; adds daily hydrographs, too large to embed |
| `webapp/explorer.html` | the only frontend, shared by both deployments |
| `ARCHITECTURE.md` | how the pieces connect and why — read before modifying anything |
| `KNOWN_ISSUES.md` | data-quality problems, unconfirmed parameters, parked decisions |

## Reading the output

Every metric is computed on **paired days only** — days where that gauge and the model both
report — so every gauge has a different sample.
