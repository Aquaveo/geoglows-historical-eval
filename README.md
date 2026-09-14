# geoglows-historical-eval

Scores modelled daily river discharge against observed stream gauges, one gauge at a time,
and publishes the result as an interactive map, a per-gauge inspector and grouped summaries.

Built to answer *"is this model any good, and where is it worst?"* for the GEOGLOWS v2
retrospective, and to compare it against alternative model runs — routed output, parameter
variants, anything that can be written as a parquet of discharge.

Currently scoped to **VPU 714** (Missouri/Mississippi) as a demo.

## What you need before running

Two inputs the scripts do not fetch:

- `master_catalog_with_metadata.xlsx` — the gauge catalog, with `final_river_id`, `gauge_id`,
  `ISO_A3`, `latitude`, `longitude`
- `routing/gauge_data/*.csv` — one CSV per gauge, columns `datetime,discharge`, named
  `{ISO_A3}_{provider}_{station}.csv`

Both live under one directory. **There is no default** — set `$GEOGLOWS_EVAL_DATA` or pass
`--data-dir`. All three scripts check it before doing any other work, so a wrong path fails
immediately rather than looking like missing gauge data. These are designed to be downloaded from
the gauge data that we have on AWS. You must have them downloaded locally to be able to access them.

The model side needs nothing prepared — the GEOGLOWS retrospective zarr is read from S3. Or it can
read your own input.

## Running it

Set the data directory once, so every script finds it in this and future shells:

```bash
echo 'export GEOGLOWS_EVAL_DATA=/path/to/your/gauge/data' >> ~/.bashrc && source ~/.bashrc
```

`export` on its own only lasts for the current terminal. Every script also takes
`--data-dir` if you would rather pass it explicitly.

```bash
conda env create -f environment.yml && conda activate geoglows-eval

python kge_map.py --vpu 714          # metrics + run config + static map
python build_webapp.py --vpu 714     # one self-contained HTML page
python serve.py --vpu 714            # or the live local app on :8765
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
