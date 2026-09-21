# geoglows-historical-eval

Scores modelled daily river discharge against observed stream gauges, one gauge at a time,
and publishes the result as an interactive map, a per-gauge inspector and grouped summaries.

Built to answer *"how well is this model performing at different gauges?"* for the GEOGLOWS v2
retrospective, and to compare it against alternative model runs — routed output, parameter
variants, anything that can be written as a parquet of discharge.

It scores in **two modes**. *Statistics* reports the usual metrics — KGE', NSE, bias,
contingency scores, all of them per calendar month as well. *Decisions* asks whether the model
would inform a real decision correctly, and answers with a verdict per gauge rather than a
number. Both come out of one run.

Currently scoped to **VPU 714** (Missouri/Mississippi) as a demo.

## What you need before running

**Gauge observations** — one CSV per gauge, columns `datetime,discharge`, named
`{ISO_A3}_{provider}_{station}.csv`.

**Use a local copy. This is the recommended way and the default.** You name two things — where
the CSVs are, and which file is the catalog. Neither is guessed, and neither has to sit in any
particular layout:

```bash
echo 'export GEOGLOWS_EVAL_GAUGE_DIR=/path/to/gauge/csvs' >> ~/.bashrc
echo 'export GEOGLOWS_EVAL_CATALOG=/path/to/your_catalog.xlsx' >> ~/.bashrc
source ~/.bashrc
```

Or per run: `--gauge-dir /path/to/gauge/csvs --catalog /path/to/your_catalog.xlsx`.

The **catalog** may be `.xlsx` or `.csv`, and can be called anything. It needs the columns
`final_river_id`, `gauge_id`, `ISO_A3`, `latitude`, `longitude`; `Koppen Group (as of 2024)`
is optional and adds a grouping to the page.

Local is faster, needs no credentials, and works offline.

*Shorthand:* if your files already sit the way `download_observed_data.py` writes them —
`<dir>/routing/gauge_data/` beside `<dir>/master_catalog_with_metadata.xlsx` — then
`--data-dir <dir>` (or `$GEOGLOWS_EVAL_DATA`) fills in both. Anything you name explicitly wins
over it.

### Reading from S3 instead

The gauges are also published to a **private** bucket, `master-gauge-data`.

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

Score the model. This always comes first, and writes the metrics parquet, the decision
verdicts, the run configs and a static PNG map into `outputs/`:

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

### The two modes

`--mode` defaults to **`both`**, which writes the metric table and the decision verdicts from
one run. Use `statistic` or `decision` to write only one of them.

In the browser, a **Mode** control switches between them and the metric picker becomes a
**Question** picker. Each decision colours the map with one verdict per gauge, and clicking a
gauge gives the arithmetic behind its colour in plain words.

### The questions decision mode answers

Five, each scored per gauge and each with its own idea of what "wrong" means:

| Question | What is compared | Metric | Where the worst band comes from |
|---|---|---|---|
| **Does the model identify severe low flow?** | HydroSOS category 1 — the driest 10% of each calendar month, each series binned on its own record | catch rate on severe months | **derived** — a per-gauge binomial test against luck |
| **Does the model show a flood when the river floods?** | floods above each series' own 2-year level, declustered, matched within ±3 days | CSI | **derived** — a per-gauge binomial test against luck |
| **Is the annual volume of water representative?** | the GEOGLOWS bias-corrected series against the gauge | PBIAS | **published** — Moriasi et al. (2007) streamflow ratings |
| **Does the model see how the river is changing?** | Mann-Kendall trend sign on annual mean flow, same window both series | trend direction agreement | significance of the trend test |
| **Does the model tell wet days from dry days?** | daily modelled flow against daily gauge flow | Spearman rank correlation | **chosen** — no benchmark behind it |

Four of the five divide magnitude out deliberately, by comparing each series against thresholds
fitted from its *own* record. That is what makes them answerable: the raw model's flood
magnitudes are off by more than a factor of two at roughly 40% of gauges, so anything resting on
absolute values fails before it starts. The volume question is the exception, and it only works
because the bias correction fixes exactly that.

The last row is the weakest — its bands are judgment with nothing behind them, and it is flagged
as such. **Check that column before quoting any verdict.**

One question is deliberately **not** answered: whether flood *magnitudes* are right. It was
measured and abandoned — demonstrating it at a single gauge needs about 145 years of overlapping
record and no gauge has 100. The skill is real when pooled across gauges; it just cannot be
resolved to an individual river. [DECISION_MODE.md](DECISION_MODE.md) has the working.

### The volume decision needs the network

One decision — *is the annual volume of water representative?* — is scored on the **GEOGLOWS
global bias correction**, because the raw model is far too biased to answer it. That is the
only part of a decision run that touches the network, so it is **off by default**:

```bash
python kge_map.py --vpu 714 --bias-correct
```

It fetches one correction per reach — **about 10 minutes for a full VPU** at 12 workers, during
which it prints nothing. The run tells you the estimate before it starts, so you can tell it
apart from a hang.

Reaches where the published SFDC table holds a zero get no corrected series and come out grey.
That share varies a lot by region — **4% to 34%** across the VPUs measured — and it is a gap in
the published correction data, not a problem with your gauges.

If you run the correction yourself rather than through `kge_map.py`, expect
`RuntimeWarning: invalid value encountered in divide` from `geoglows/bias.py`, once per reach
and month with a zero scalar. It is that same condition and it is harmless. `kge_map.py`
silences it around that call only, detects the affected reaches and reports them as unusable,
rather than letting thousands of warning lines bury the output.

What each decision asks, what it compares and where its bands come from is in
[DECISION_MODE.md](DECISION_MODE.md), along with every provisional number and what it costs.

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
| `DECISION_MODE.md` | what each decision asks, every threshold behind it, and what each one costs |

## Reading the output

Every metric is computed on **paired days only** — days where that gauge and the model both
report — so every gauge has a different sample.
