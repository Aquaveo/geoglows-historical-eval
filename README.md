# geoglows-historical-eval

Scores modelled daily river discharge against observed stream gauges, one gauge at a time,
and publishes the result as an interactive map, a per-gauge inspector and grouped summaries.

Built to answer *"is this model any good, and where is it worst?"* for the GEOGLOWS v2
retrospective, and to compare it against alternative model runs — routed output, parameter
variants, anything that can be written as a parquet of discharge.

Currently scoped to **VPU 714** (Missouri/Mississippi) as a demo.

## What you need before running

**Gauge observations** — one CSV per gauge, columns `datetime,discharge`.

These are read straight from S3 by default; nothing is downloaded and nothing is cached.
The bucket (`master-gauge-data`) is private, so you need AWS credentials with
`s3:ListBucket` on it and `s3:GetObject` on `production/*`. 

Credentials are picked up the usual way — a `[default]` profile, `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` in the environment, an attached instance role, or
`--aws-profile some-name`. Check yours works before running anything; if this works, the
scripts will work:

```bash
aws s3 ls s3://master-gauge-data/production/ --profile your-profile
```

If you already have the CSVs, point `--data-dir` (or `$GEOGLOWS_EVAL_DATA`) at the folder
holding `routing/gauge_data/`. Local is used by default when that folder is there, and
it is faster. Otherwise, aws will be used by default. `--gauge-source local` or `--gauge-source s3` forces the choice either way. The flag is optional

**Reading from S3 needs nothing local at all**

Which source ran is recorded in `vpu<VPU>_run.json`, along with the bucket snapshot date, so
two sets of results are either comparable or provably not.

**The model** — nothing to prepare. The GEOGLOWS retrospective zarr is public HTTP with no
credentials, or pass `--model-parquet` to score your own run from a local source.

## Running it

```bash
conda env create -f environment.yml && conda activate geoglows-eval
```

Score the model. This always comes first, and writes the metrics parquet, the run config and a
static PNG map into `outputs/`:

```bash
python kge_map.py --vpu 714 --aws-profile your-profile
```

Then look at the results — **start the server**:

```bash
python serve.py --vpu 714 --aws-profile your-profile
```

Open <http://localhost:8765>. This is the way to use the tool day to day: it is the full
version, and the only one that draws each gauge's **daily hydrograph**, read live when you
click. It never writes a file, so re-running it costs nothing.

### Only when you need to send it to someone

`build_webapp.py` is **not a step in the pipeline** — it is a second, independent way of
viewing the same metrics parquet, and most of the time you will not need it:

```bash
python build_webapp.py --vpu 714 --aws-profile your-profile
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
| Viewer needs Python + AWS | yes | no |

Reading a local copy of the gauges instead — set it once so every script finds it in this and
future shells, since `export` on its own only lasts for the current terminal:

```bash
echo 'export GEOGLOWS_EVAL_DATA=/path/to/your/gauge/data' >> ~/.bashrc && source ~/.bashrc
```

## Comparing several model runs

Every model gets scored separately and viewed separately. Nothing is shared between runs
except the gauges they are scored against.

**Score each one into its own `--outdir`**, or the second overwrites the first:

```bash
python kge_map.py --vpu 714 --aws-profile your-profile
```

```bash
python kge_map.py --vpu 714 --model-parquet routed.parquet --label "routing v7" --warmup-years 1 --outdir outputs_v7 --aws-profile your-profile
```

**Then serve each on its own port** and put the two browser tabs side by side:

```bash
python serve.py --vpu 714 --port 8765 --aws-profile your-profile
```

```bash
python serve.py --vpu 714 --metrics outputs_v7/vpu714_metrics.parquet --port 8766 --aws-profile your-profile
```

`--label` names the run on its page, its browser tab and the map footer. **Set it on every
non-default run.** Without it the two tabs are identical apart from their numbers and both
claim to be GEOGLOWS v2 — which is exactly the confusion a comparison is meant to resolve.

If instead you want to *send* both runs to someone, build a file per run. `build_webapp.py`
has no `--outdir`, so point `--metrics` and `--out` into each directory yourself:

```bash
python build_webapp.py --vpu 714 --aws-profile your-profile
```

```bash
python build_webapp.py --vpu 714 --metrics outputs_v7/vpu714_metrics.parquet --out outputs_v7/vpu714_explorer.html --aws-profile your-profile
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
