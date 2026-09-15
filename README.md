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

**Reading from S3 needs nothing local at all.** 

Which source ran is recorded in `vpu<VPU>_run.json`, along with the bucket snapshot date, so
two sets of results are either comparable or provably not.

**The model** — nothing to prepare. The GEOGLOWS retrospective zarr is public HTTP with no
credentials, or pass `--model-parquet` to score your own run from a local source.

## Running it

```bash
conda env create -f environment.yml && conda activate geoglows-eval
```

With bucket credentials and nothing downloaded:

```bash
python kge_map.py --vpu 714 --aws-profile your-profile
```

```bash
python build_webapp.py --vpu 714 --aws-profile your-profile
```

```bash
python serve.py --vpu 714 --aws-profile your-profile
```

`kge_map.py` writes the metrics, run config and static map; `build_webapp.py` turns those
into one self-contained HTML page; `serve.py` serves the same page live on :8765 with
daily hydrographs, which are too large to embed.

Reading a local copy instead — set it once so every script finds it in this and future
shells, since `export` on its own only lasts for the current terminal:

```bash
echo 'export GEOGLOWS_EVAL_DATA=/path/to/your/gauge/data' >> ~/.bashrc && source ~/.bashrc
```

Scoring a different model, into its own directory so the baseline survives:

```bash
python kge_map.py --vpu 714 --model-parquet routed.parquet \
    --label "routing v7" --warmup-years 1 --outdir outputs_v7
python build_webapp.py --vpu 714 --metrics outputs_v7/vpu714_metrics.parquet \
    --out outputs_v7/vpu714_explorer.html
```

`--label` names the run on the page, its browser tab and the map footer. Set it whenever the
discharge is not the default retrospective, or two runs produce outputs identical apart from
their numbers and both claim to be GEOGLOWS v2.

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
