# Known Issues / Deferred Decisions

Data-quality and methodology issues found while scoping the RFS retrospective evaluation.
**None of these block the VPU 714 demo.** They are recorded here so the demo's caveats are
explicit and so we can come back and fix them before the evaluation is used for real
conclusions.

Status legend: `DEFER` = fine to ignore for the demo · `CAVEAT` = must be stated on any
chart/report produced · `FIX-BEFORE-GLOBAL` = must be resolved before extending past VPU 714.

Measured 2026-09-02 against:
- `<data-dir>/master_catalog_with_metadata.xlsx` (37,528 gauges)
- `<data-dir>/routing/gauge_data/` (~8,140 CSVs)
- `http://geoglows-v2.s3-website-us-west-2.amazonaws.com/` → `tables/v2-model-table.parquet`

---

## 1. Large rivers are unevaluated — stream orders 8 and 9 have zero gauges

`FIX-BEFORE-GLOBAL`

VPU 714 contains 1,521 order-8 and 594 order-9 reaches, but **not one of them carries a matched
gauge**. Gauged stream orders run 2–7 only (2:185, 3:592, 4:895, 5:655, 6:261, 7:44), and gauged
`USContArea` tops out near 4.9e10 m² (~49,000 km²).

The cause is not established.

- **Impact**: any statement about model performance on large rivers is unsupported. Stream-order
  stratification will silently stop at order 7.
- **Deferred decision**: investigate whether main-stem gauges are (a) assigned
  `final_river_id = -1`, (b) snapped to a small tributary reach instead of the main stem, or
  (c) assigned to a neighbouring VPU.

## 2. `final_river_id = -1` sentinel on 31% of the global catalog

`FIX-BEFORE-GLOBAL`

11,639 of 37,528 catalog gauges have `final_river_id = -1`, plus 195 with `0`. These are
unmatched sentinels, not reaches. Usable global catalog is ~25,700 gauges.

- **Impact**: none on VPU 714 (we filter `final_river_id > 0` before the VPU join).
- **Deferred decision**: whether these can be re-matched, and whether the failures are
  systematically biased toward large rivers (see issue 1) or toward specific providers/regions.

## 3. Stage-only gauges masquerade as discharge gauges

`DEFER`

5 of the 2,632 VPU 714 files have a `water_level` column instead of `discharge` — 2 HYDAT and
3 USGS. Globally the catalog flags 8,218 gauges with `water_level` and 1,258 *without*
`discharge`.

- **Impact**: minor — 5 of 2,632 in VPU 714. Usable discharge gauges = **2,627**.
- **Resolved for S3 runs**: the bucket's per-provider `catalog.csv` carries `discharge` and
  `water_level` flags, and `S3GaugeSource.catalog()` now drops stage-only gauges before any
  file is opened — 4,182 of them globally. A local run still discovers it at read time, by
  finding no `discharge` column.

## 4. Multiple gauges on one reach — no dedup policy

`DEFER`

VPU 714: 30 reaches carry more than one gauge (61 gauges involved; the worst carries 3).
Globally: 1,213 reaches, 14,334 gauge rows.

- **Impact**: small in VPU 714, but it means the same model series is scored more than once,
  slightly over-weighting those reaches in aggregate statistics.
- **Deferred decision**: pick-one policy (longest record? closest to the reach outlet? highest
  data completeness?) or keep all and weight by `1/n_gauges_on_reach`.

## 5. Zero and negative flows break log-space and relative metrics

`CAVEAT`

Measured on the current 2,585-gauge run (full model record):

| | gauges | |
|---|---|---|
| no zero-flow days | 1,561 | 60.4% |
| >1% zero days | 575 | 22.2% |
| >10% zero days | 221 | 8.5% |
| >30% zero days | 91 | 3.5% |
| >50% zero days | 49 | 1.9% |

Globally it is worse — sampled records hit 62%, 35%, 23%.

**Nothing currently computed needs a zero-flow policy.** KGE′ and its components, NSE, RMSE,
MAE, the correlations and the contingency scores are all defined at Q = 0; a zero is just a
number. Only log-space and relative metrics break — log-NSE, MAPE, MALE, 1/Q transforms — and
none of those is computed, so the 221 gauges above cause no trouble today. This note lived in
`kge_map.py`; it belongs here because it is about a decision, not about what the code does.

- **Impact**: the decision becomes live the moment any log-space or relative metric is added.
  At that point it is the single biggest metric-design choice, because dropping zeros changes
  which days are scored and biases low-flow conclusions.
- Related: `metrics_from_pair()` guards each metric on its own divisor, so a gauge whose record
  is entirely zero still yields rmse and mae instead of being dropped. See §L.
- **Deferred decision**: pick and document one policy — epsilon offset `log(Q + eps)` with a
  stated `eps`, drop-pairs, or route zero-flow behaviour to a dedicated dry/wet contingency
  metric instead of a continuous one. **Whatever we pick must be recorded per chart**, because
  it is not comparable across choices.

## 6. Small streams dominate the gauge sample

`CAVEAT`

A quarter of VPU 714 gauges have median flow below 0.53 m³/s (overall median of per-gauge
medians: 2.15 m³/s; p95: 54 m³/s).

- **Impact**: aggregate metrics will be dominated by small headwater catchments, which is where
  a globally-routed model performs worst. A poor headline KGE may say more about the sample than
  the model.
- **Deferred decision**: stratify by drainage area (`USContArea`) or median flow as a primary
  grouping, not just stream order — area is continuous and avoids order's binning artifacts.

## 7. Daily-value definition mismatch (unverified)

`CAVEAT`

GEOGLOWS v2 `daily.zarr` is daily-mean discharge. It is **not yet verified** that the gauge CSVs
are also midnight-to-midnight daily means — USGS daily values are, but provider conventions vary
(some are 9am-to-9am; some records are instantaneous readings).

- **Impact**: a window mismatch produces systematic bias and damped peaks that get misattributed
  to the model. Matters most for peak magnitude and timing metrics.
- **Deferred decision**: confirm per provider. Low risk in VPU 714 (99% USGS).

## 8. Timezone alignment (unverified)

`DEFER`

Model timestamps are UTC. Gauge timestamps are assumed local-standard-time but unverified.

- **Impact**: negligible at daily resolution (sub-one-day). **Becomes real if we move to
  hourly** — a 5–8 hour offset across the VPU would corrupt every timing metric.
- **Deferred decision**: resolve before the hourly migration, not before the demo.

## 9. Dirty `Continent` labels in the catalog

`FIX-BEFORE-GLOBAL`

Both `North_America` (9,845) and `North America` (6,358) exist, likewise `South_America` /
`South America`.

- **Impact**: none in VPU 714 (single continent). Groups silently split in two if used globally.
- **Deferred decision**: trivial normalization when continent grouping is first needed.
  `Koppen Group (as of 2024)` is clean by contrast and is the better ready-made stratifier.

## 10. Severe geographic bias in the downloaded set

`CAVEAT` (global only)

Of the 8,140 downloaded CSVs, ~87% are USA (3,585), France (1,929), Spain (863), Brazil (728).
Africa and Asia are nearly absent.

- **Impact**: no aggregate over this set may be labelled "global performance."
- **Deferred decision**: pull more gauges per VPU, or explicitly frame results per-VPU /
  per-Köppen-zone rather than as a single global number.

## 11. VPU 714 is an unusually favourable test bed

`CAVEAT`

2,012 of 2,627 gauges have **30+ years** of overlap with the model period; only 11 have under 5
years. Köppen coverage is Continental/Temperate/Arid only — no Tropical or Polar.

- **Impact**: metrics needing long records (return periods, extreme-value fits, decadal
  trend-in-skill, seasonal/anomaly decomposition) are all viable here and will *not* be in
  data-poor VPUs. Demo results will look better than global results.
- **Deferred decision**: when generalizing, re-check that each metric has enough record length
  in the target VPU before computing it.

## 12. Short records and record-length sensitivity (global)

`FIX-BEFORE-GLOBAL`

Globally, record lengths run from 624 days to 108 years, and some records end before the model
period begins (e.g. 1918, 1930) — zero overlap.

- **Impact**: KGE and especially return-period estimates are unstable on short records; mixing
  them with 50-year records in one aggregate is not defensible.
- **Deferred decision**: a minimum-overlap threshold, and record length as a reported stratifier.

## 13. Missing data within records

`DEFER`

VPU 714: median 0% missing, p90 2.1%, p99 35%, max 85%.

- **Impact**: gaps that cluster seasonally bias monthly and seasonal metrics.
- **Deferred decision**: minimum completeness threshold, and a minimum-days-per-stratum rule
  before reporting a per-month or per-group metric.

## 14. Unconfirmed units on `USContArea`

`DEFER`

Treated as m2 in the code. VPU 714 max is 4.9e10. Not confirmed against documentation.

- **Impact**: only affects axis labels and area-bin boundaries, not metric values.

## 15. Gauge observation uncertainty is unaccounted for

`CAVEAT`

Rating-curve error, high-flow extrapolation beyond the measured range, ice-affected winter
records (relevant — 1,243 VPU 714 gauges are Köppen Continental), and datum shifts are all
unquantified. Observations are treated as truth throughout.

- **Impact**: sets a floor on achievable skill that we are attributing entirely to the model.
  Worst at exactly the high flows we most want to evaluate.
- **Deferred decision**: not made.

---

# Open questions and suggestions — NOT implemented

Things raised in discussion and deliberately **not** built. Nothing here is in the code. Each
is parked until you decide on it.

## A. Anomaly correlation (ACC) — proposed, then removed

I added this without being asked and removed it again. Recording why it came up so it is not
lost.

The question it answers: how much of the model's correlation is genuine event skill versus just
knowing what time of year it is. You compute it by subtracting the same day-of-year climatology
from both series and correlating the residuals.

Measured on a 500-gauge sample: model r **0.480**, anomaly correlation **0.440**, seasonal
cycle **~4%** of daily variance, and a zero-skill day-of-year lookup table achieves r **0.215**.
Not computed for other regions or timescales.

## B. Spearman ρ is blind to bias correction

`spearman` is now computed, and it is higher than Pearson at **80%** of gauges (median +0.078)
— useful evidence that the model ranks days better than it sizes them.

But Spearman is invariant to any monotone transform. A per-reach quantile-mapping or SABER-style
correction **cannot change it at all**. So it is a good diagnostic and a useless verdict on a
bias correction. When variant comparison arrives, either exclude it or label it explicitly as an
invariance check — a null result there is expected, not a finding, and a *non*-null result is a
bug signal.

## C. `ss_r` — the skill score for r, with caveats

Implemented as `(r_model − r_clim) / (1 − r_clim)`. Median 0.301, positive at 86% of gauges.
Read it cautiously:

- Skill scores are properly defined on loss functions; r is not one.
- Differences in r are not comparable across its range — 0.8 → 0.9 is a far bigger real gain
  than 0.4 → 0.5 — so a normalized r-difference is hard to interpret.
- The denominator shrinks at strongly seasonal gauges, making the score volatile there.

## D. NRMSE trades a size confound for a variability one

Measured on this network: RMSE correlates with drainage area at Spearman **+0.754**; NRMSE (by
mean flow) drops that to **−0.145**, so the size dependence is genuinely fixed. But NRMSE
correlates with the observed CV at **+0.818**, so flashy and arid gauges now look worse at equal
model quality — and the High Plains, the worst-performing region, is also the flashiest. Not a
bug, but it should be stated wherever NRMSE is mapped.

Note also that RMSE/σ was **not** used, because it equals √(1−NSE) exactly (verified to 1e-16)
and would have duplicated NSE.

## E. The dominant error is amplitude, not timing — which changes the bias-correction outlook

Evidence: Spearman > Pearson at 80% of gauges; anomaly correlation 0.440 versus model r 0.480;
median β 0.70 and γ 0.80; FDC high-flow end clipped; only 53% of gauges beat the seasonal
climatology on a squared-error basis despite decent correlation. The model gets the timing
roughly right and the magnitudes wrong.

This bears on an earlier caution: bias correction cannot change correlation, so it would not
help if timing were the dominant error. The measurements above show the correlation terms are
the stronger part and the magnitude terms (beta, gamma) are the weaker ones.

What follows from that is a matter of arithmetic only: a monotone per-reach transform can move
beta and gamma, and cannot move r or Spearman. Whether correcting beta and gamma actually
raises KGE' by a useful amount on this network has NOT been tested -- that requires running a
correction and re-scoring.

## F. Issue 5 above (zero-flow) is framed wrongly and should be rewritten

It is written as "pick a global zero-flow policy". That is not right: zero handling is a property
of each individual metric, so metric selection comes first. Most of the metric set — KGE′, NSE,
r, Spearman, RMSE, MAE, PBIAS, contingency scores — is perfectly defined at Q = 0. Only the log
and relative family breaks (log-NSE, MAPE, MALE, 1/Q transforms, FLV, low-flow quantile ratios).

There is one genuinely cross-cutting decision hiding in it: whether all metrics share one common
valid-day mask, or each uses its own. Not decided, not implemented. If masks differ per metric,
two metrics on the same gauge describe different day sets.

Separately: RFS routing essentially never outputs zero discharge, so any dry/wet contingency
score, no-flow-spell statistic, or intermittency classification measures a known structural
absence rather than model performance.

## G. Operational gotcha: stale server holds the port

If `serve.py` appears to ignore your changes, check for an orphaned process on 8765 — a second
instance fails to bind and the browser is silently answered by the old one. This produced a
false "the new fields are missing" result during development.

```bash
pgrep -af "[s]erve\.py"      # find it
ss -ltn | grep 8765          # confirm what holds the port
```

## H. The divisors in ss_clim and NSE are built from the same gauge

DECIDED: no donor-transfer benchmark. Recorded because the limitation still applies to
what is being reported.

The day-of-year climatology used by `ss_clim` is built from **the target gauge's own
observed record**. It therefore cannot be computed at an ungauged reach, which is where
the model is actually used. As the user put it: comparing to climatology requires having
gauge data, so it does not prove the model adds anything where it matters.

The same applies to **NSE**, which divides by var(obs) -- identically the squared error of
predicting that gauge's mean every day. Both are still reported. This is not a defect in the
metrics: at a gauged reach both divisors are genuinely available, and normalising by observed
variance is standard practice. The limit is on extrapolation, not on validity.

What `ss_clim` legitimately supports: "the model carries more information than the season
alone" -- a statement about model content, not about deployment value. It should not be
quoted as evidence the model beats the alternatives at ungauged sites.

**Rejected fix (do not build unless asked):** nearest-donor transfer -- predict each gauge
from the closest *other* gauge scaled by drainage-area ratio, which needs nothing from the
target gauge, so unlike the two above it can be formed at an ungauged reach. Everything required
is already present (lat/lon, `USContArea`, 2,357 gauges in the VPU). The user has declined
it.

Note that the project's central question is unaffected: "is v2 better than v1" compares two
models, neither of which is built from the target gauge, so this limitation does not apply.

## I. Glyphs above U+2000 are a font-coverage risk

IBM Plex does not cover `U+25C0`/`U+25B6` (triangles), `U+2922` (fit arrow), `U+2192`
(right arrow) or `U+2212` (minus). Using them made the browser substitute a different font
for those characters only, which reads as "weird symbols" and is easy to mistake for an
encoding fault. It is not the same problem as the missing `<meta charset>` in issue J.

Rule: icons are inline SVG, not typed characters. Only `- - ... >=` style punctuation
(`U+2014`, `U+2013`, `U+2026`, `U+2265`) plus degree, middot, superscripts and Greek
letters are known safe here. Audit with:

```bash
python -c "s=open('webapp/app.html',encoding='utf-8').read(); print({hex(ord(c)):s.count(c) for c in set(s) if ord(c)>0x2000})"
```

## J. A missing charset declaration is a separate failure mode

The pages are valid UTF-8 on disk, but a document with no `<meta charset="utf-8">` opened
over `file://` gets no encoding declaration and the browser guesses -- Windows-1252 here --
turning every degree sign, middot and superscript into mojibake. Over HTTP `serve.py` sends
the charset in a header and the Artifact wrapper injects one, so only the local-file path
broke. Both templates now carry the meta tag, and `build_webapp.py` reads and writes with
explicit `encoding="utf-8"` rather than the locale default.

## K. Heavy tails make ECDF plots unreadable, and unbounded metrics need care

On a fixed -1..1 axis these fractions of the distribution fall off the left edge entirely:
KGE' 10.1%, NSE 18.8%, `ss_clim` 22.3%. Worst single-gauge values are -300, -10601 and
-9548. Any plot or scale built on the display range silently hides that tail, and any mean
over these columns is meaningless -- always the median.

The per-gauge distribution plot was removed for this reason and replaced with a percentile
sentence that positions by **rank**, not by value, which is immune to the tail. The same
plot still appears in the map rail's population view and has the same problem.


## L. Fixed: the zero-variance guard discarded metrics that need no variance

Was: `pair_stats()` had a single early `return out` on zero variance, which threw away rmse,
mae, beta, pbias_pct, nrmse and mae_rel -- none of which needs a variance -- and dropped the
gauge from the map and the summary entirely, so the worst-behaved reaches were excluded rather
than counted.

Fixed by extracting `metrics_from_pair()`, called by both `pair_stats()` and `monthly_stats()`,
with per-metric guards: rmse and mae always written, alpha and nse when sd_obs > 0, r and
spearman when both sds > 0, beta/pbias_pct/nrmse/mae_rel when mean_obs != 0, gamma and kge_2012
when both hold.

Not latent: **47 gauge-months on VPU 714 have an all-zero observed record** and now carry
rmse and mae where previously the monthly path wrote them and the annual path would not have.
Verified the refactor changed no existing value -- 271 columns, 2,357 rows, zero differences.


## M. Metrics deliberately not computed

Moved out of `kge_map.py`: the script documents what it does, not what it does not.

| Metric | Why not |
|---|---|
| KGE (Gupta 2009) | Only KGE′ is used. Carries nothing beyond (r, alpha, beta), and charges a biased model twice — once through beta, again through a shrunken alpha. Median beta here is ~0.70. |
| `ss_r` | A skill score on r. Non-standard: skill scores are defined on loss functions and r is not one. |
| ACC (anomaly correlation) | Measured at 0.440 against r 0.480 on a 500-gauge sample; seasonality is ~4% of daily variance here. Not requested. |
| MAPE | Divides by the observation. 223 gauges have >10% zero-flow days and a quarter have median flow under 0.53 m³/s. |
| log-NSE, MALE, 1/Q transforms | Need an explicit zero-flow policy, not yet decided. See §F. |
| RPS, CRPS, Brier, reliability diagrams, rank histograms, ROC/AUC | Require a predictive distribution. |
| Donor-gauge transfer | Needs nothing from the target gauge, so unlike `nse` and `ss_clim` it could be formed at an ungauged reach. Declined; see §H. |

Separately, three columns **are** computed and stored but are not shown in the interface,
because each duplicates a sibling: `ss_clim_mae` (vs `ss_clim`), and `rmse` and `mae` (vs
`nrmse` and `mae_rel`). They stay in the parquet so nothing is lost to a display decision.

## N. Why ss_clim and ss_clim_mae disagree is not established

Measured on VPU 714: median `ss_clim` +0.037 (53% of gauges above 0) against median
`ss_clim_mae` +0.120 (63% above 0), with the MAE version higher at 75% of gauges. So the model
is relatively worse against climatology on squared error than on absolute error. The cause is
not known.

Two explanations were proposed and both were refuted. Recorded so they are not retried:

- *"The model clips peaks, so it loses more on MSE."* Refuted — that predicts the gap grows as
  alpha falls, but Spearman(gap, alpha) = **+0.483**, the opposite sign.
- *"Squaring hurts the model more than it hurts the reference."* Refuted — of each series' own
  MSE, the top 1% of observed-flow days contribute **39%** for the model against **59%** for
  climatology, so squaring concentrates the *reference's* error on floods more.


## O. Chosen parameters, none confirmed — review later

Every value here was picked while building, not derived and not agreed. Measured against the
current VPU 714 run (1940-01-01 to 2026-08-29, 2,585 gauges) so the cost of each is visible.
They bind far harder on a VPU with shorter records.

### `--min-years` = 1
Minimum years of paired overlap before a gauge is kept. **Changed from 10 to 1.** The old
value was picked while building and never derived; 1 was chosen deliberately, to favour
coverage and to match the `MIN_DAYS = 365` used by the RAT evaluation scripts.

| threshold | gauges surviving |
|---|---|
| 0 | 2,585 |
| 10 (old default) | 2,584 |
| 15 | 2,511 |
| 20 | 2,407 |
| 30 | 1,990 |

(The sweep predates the change of default and has no row for 1. It sits between the 0 and 10
rows, so it admits either 2,585 or 2,584 — not re-measured.)

On this VPU's full 86-year record the threshold is nearly inert either way — 10 excluded
exactly one gauge. It binds hard on a short record: over 2000–2024 the same 10-year rule
dropped **657** of 2,627 gauges, which was most of the gauge-count gap against RAT.

**What one year costs, measured.** 291 gauges with at least 10 near-complete calendar years
each, KGE′ computed per year and compared with that gauge's whole-record value:

| | |
|---|---|
| median \|single-year − full-record\| | 0.175 |
| 90th percentile | 1.463 |
| single years landing >0.1 from the full value | 66.9% |
| single years landing >0.3 away | 32.1% |
| median spread across one gauge's own years | 1.174 (IQR 0.303) |

45.4% of gauges that clear the −0.41 no-skill line on their full record fail it in at least
one individual year. So a one-year gauge is admitted with a KGE′ whose value depends heavily
on which year it happens to be.

Two limits on that measurement. It samples gauges that *have* long records, and gauges holding
only one year may not behave the same. And it argues against 1 being *precise* — it does not
identify a better threshold, and nothing measured here distinguishes 5 from 10 from 15.

Two knock-on effects of the lower default:
- `MIN_YEARS_FOR_RETURN` is still 10, so gauges admitted with short records get no `t2_obs`,
  `t2_sim`, POD, FAR, CSI, ETS or freq_bias. Those columns are simply absent for them.
- `MIN_DAYS_PER_MONTH` = 60 means a gauge with one year (about 30 days per calendar month)
  reports no monthly metrics at all.

### `MIN_DAYS_PER_MONTH` = 60
A calendar month with fewer paired days than this reports only its day count, no metrics.

**No derivation behind the number.** Chosen as "small relative to a typical month's sample".
The median gauge-month here holds 1,590 days, so 60 is about 4% of typical. A defensible
alternative would tie it to years rather than days — e.g. require 10 Januaries — since the
monthly statistics pool across years and it is the number of *years* that determines how well
determined they are.

| threshold | gauge-months suppressed, of 31,020 |
|---|---|
| 30 | 26 |
| 60 (current) | 42 |
| 90 | 49 |
| 180 | 96 |
| 365 | 301 |

### `MIN_YEARS_FOR_RETURN` = 10
Minimum annual maxima before a 2-year return level is estimated. No gauge on this VPU falls
below it; 2 gauges lack contingency scores for a different reason (an all-zero observed record,
so no positive flood level exists). The 2-year level is the median of the annual maxima, which
is the best-determined quantile available, so 10 is not obviously too low — but it was not
tested.

### Annual maxima by calendar year
**Decided.** Calendar year, not water year, because the water year is defined differently by
region and hemisphere and would not be comparable across VPUs. Consequence: a basin whose flood
season spans the new year can have one event split across two years.

### Duplicate gauges on a reach: keep the longest paired record
Where several gauges map to one reach, one is kept. Alternatives not evaluated: nearest to the
reach outlet, highest completeness, or keeping all and weighting by `1/n`.

### `--vpu` default = 714
Convenient here, but arbitrary for anyone else running the script. Consider requiring it.


## P. Return-period thresholds: daily vs hourly, and what still needs checking

**Decided and implemented.** The 2-year flood thresholds behind `pod`/`far`/`csi`/`ets` use a
Gumbel Type-I fit by method of moments, calling `geoglows.analyze.gumbel1()` directly so the
method cannot drift from RFS. Annual maxima are taken by calendar year from **each series'
whole record inside the evaluation window**, not from the days the two series share. This is
the method RFS itself uses, so the threshold is comparable with the return periods GEOGLOWS
publishes.

### Why each series is tested against its own threshold

A model with a volume bias would seldom reach the *observed* threshold at all, and every score
would collapse into a restatement of that bias. Comparing each series to its own 2-year level
asks a cleaner question — on the days the gauge calls a 2-year flood, does the model also call
one? — and is invariant to systematic bias.

The consequence is that the two series can flag **different numbers of days**. A 2-year level
is exceeded in about half of years, but a flood spans several days, so the count of exceedance
*days* depends on peak width and is not fixed by the threshold. These are therefore not purely
a timing test. `freq_bias` is stored so that can be read off rather than assumed — see item 4.

### The published return periods come in three variables, and the package exposes the wrong one

`retrospective/return-periods.zarr` carries `gumbel`, `gumbel_daily` and `gumbel_hourly`.
Measured on 12 reaches: **`gumbel` and `gumbel_hourly` are identical**, and both run **12.6%
above `gumbel_daily`**, because hourly peaks exceed daily means.

`geoglows.data.return_periods()` returns `gumbel` — i.e. the hourly one — with nothing in its
signature or output saying so, and it rejects `distribution="gumbel_daily"` as unrecognised.
**Anything comparing thresholds against that function will look ~12.6% off for reasons that have
nothing to do with the model.** This evaluation is daily throughout, so `gumbel_daily` is its
counterpart.

Verified after the change: `t2_sim` matches published `gumbel_daily` at **−0.03% median, within
5% at 40/40 reaches, worst 2.2%**. The residual is the window edges.

### What changed, and by how much

| | before | after |
|---|---|---|
| annual maxima from | paired days only | each series' whole record |
| `t2_sim` vs `gumbel_daily` | ~−5% | −0.03% |
| gauges scored | 2,583 | 2,585 |
| median POD / FAR / CSI / ETS | 0.151 / 0.782 / 0.091 / 0.088 | 0.145 / 0.795 / 0.087 / 0.084 |

### Still to look at

1. **The two sides now come from different periods.** Each threshold uses its own series' whole
   record, so a gauge covering 1990–2010 gets a threshold from 20 years while the model's comes
   from 86. If the flow regime shifted, the two levels describe different eras. Not quantified.
2. **Exceedance is counted per DAY, not per event.** One flood spans several consecutive days,
   so every count is inflated and the scores measure day-agreement rather than event-agreement.
   Declustering (a minimum separation between events) would change all four scores. Not done.
3. **Only T=2 is computed.** The published zarr also carries 5, 10, 25, 50 and 100. Scores at a
   rarer threshold would say something different — and would need the `MIN_YEARS_FOR_RETURN`
   guard revisited, since a 100-year level from 40 annual maxima is extrapolation.
4. **`freq_bias` is 0.747, not 1.** The two series flag different numbers of days even with
   matched return levels, so these are not a pure timing test. Cause not investigated.
5. **REVISIT: should the threshold be fitted on PAIRED DAYS ONLY after all?** This was
   switched away from paired days (see the table above) to make `t2_sim` match the published
   `gumbel_daily`, and it does. But that reasoning optimised for agreement with a published
   number, not for the question the contingency scores are asking, and it is what creates
   item 1 — the two thresholds now come from different periods.

   The trade-off, from the measurements already recorded above:

   | | paired days | whole record (current) |
   |---|---|---|
   | both thresholds describe the same period | yes | **no** |
   | `t2_sim` vs published `gumbel_daily` | ~−5% | −0.03% |
   | gauges scored | 2,583 | 2,585 |
   | median POD / FAR / CSI / ETS | 0.151 / 0.782 / 0.091 / 0.088 | 0.145 / 0.795 / 0.087 / 0.084 |

   Arguments each way, neither settled:
   - **Whole record**: a flood level is a property of a river, so the model's threshold should
     not move because a gauge happened to be offline. Also reproduces the published value,
     which makes the numbers checkable against something external.
   - **Paired days**: the contingency table is only ever evaluated on paired days, so a
     threshold fitted on a different sample is being applied to a sample it was not derived
     from. On a gauge with a short or seasonally-biased record the two periods can have
     genuinely different flow regimes, and then neither threshold means what it appears to.

   Note the scores move very little either way — the largest shift in the table is FAR by
   0.013 — so this is a question about what the number *means*, not about whether the headline
   changes. Deciding it needs a view on whether these scores are meant to be comparable with
   published GEOGLOWS return periods or self-contained within the paired sample.

   The choice is one line in `contingency_stats()`: it currently receives `sim_full` and
   `obs_full` (each series' whole record) from `compute_metrics()`, and would instead receive
   `both["sim"]` and `both["obs"]`.


## Q. `--min-years` counts paired days, not elapsed years

`compute_metrics()` keeps a gauge when `len(both) >= round(min_years * 365.25)`. That is a count
of paired days, so a gauge whose observations are scattered thinly across many decades passes a
filter named "minimum years of overlap".

Measured on the 2,585-gauge run:

| paired days as a share of the gauge's own span | gauges |
|---|---|
| under 60% | 149 |
| under 40% | 21 |

Worst cases run to about 4,600 paired days spread over 82 years (15% dense) and 5,200 days
over 74 years (19%). Individual stations are not named here — see the note on gauge
identifiers at the end of this file.

Whether this matters depends on the metric. For KGE', r and the error magnitudes a day is a day
and sparseness is harmless. It is not harmless for anything seasonal, and it interacts with
§P: the annual maxima behind the flood thresholds come from calendar years that may hold only a
handful of days each.

Options, none chosen: leave as is; require a minimum density; count distinct calendar years
instead of days; or require a minimum number of days per year (which would also address the
partial-year bias in §R).

**Not a bug, and not the same as the reviewer's claim** that `int(round(10 * 365.25))` yields
3653 and rejects genuine 10-year records. It yields **3652** — Python rounds 3652.5 to even —
and any ten consecutive calendar years has 3652 or 3653 days, so both are kept. The smallest
`n_pairs` in the current output is exactly 3652, admitted at the boundary.


## S. The xlsx matches 840 gauges the bucket marks `-1` — they are duplicates

`RESOLVED` — recorded because the wrong reading of it is very easy to reach

Gauges are now read from `s3://master-gauge-data`, using the per-provider `catalog.csv` files
instead of `master_catalog_with_metadata.xlsx`. The two catalogs are the same size — 37,529
rows against 37,528 — and `final_river_id` agrees on every gauge they both match. They do not,
however, match the same gauges.

Measured 2026-09-14 across all 154 prefixes at tag `20251008`, on the 37,027 gauges both know:

| | gauges |
|---|---|
| matched to a reach in both | 24,454 |
| matched in the xlsx, sentinel `-1` in the bucket | 840 |
| matched in the bucket, not in the xlsx | 1 |
| unmatched in both | 11,732 |

**The 840 are not a gap in the bucket. They are the same stations twice.** 839 are CARAVAN,
and CARAVAN is an aggregation that republishes national datasets under prefixed ids:

| prefix | gauges | upstream |
|---|---|---|
| `hysets_` | 545 | HYSETS — which is itself built on HYDAT |
| `camelsaus_` | 148 | CAMELS-AUS — BoM |
| `lamah_` | 75 | LamaH — central Europe |
| `camelsbr_` | 71 | CAMELS-BR — ANA |

Strip the prefix and **492 of the 839 already exist in the bucket, matched, under their native
provider** (HYDAT 308, BoM 118, ANA 65, SENAMHI 1) — and **353 of those resolve to the same
reach**. The pattern is exact: a CARAVAN id is its native id with a source prefix bolted on,
so `<prefix>_<native id>` and `<native id>` are one station carrying one `final_river_id`
between them. The `-1` on CARAVAN is deliberate de-duplication.

### Why overriding it was worse than doing nothing

An earlier version of `S3GaugeSource._enrich()` filled these matches in from the xlsx. On
VPU 714 that added 6 gauges — and took duplicates-on-a-shared-reach from **1 to 31**.
`compute_metrics()` breaks those ties with `drop_duplicates("final_river_id", keep="first")`
after sorting by `n_pairs`, so the longest record wins: a CARAVAN republication can displace
the native gauge and silently change which station a reach is scored against. The enrichment
was removed; the bucket is authoritative for matching, including where it declines to match.

The remaining 347 have no native counterpart in the bucket. They are either genuinely unique
or their native copy is itself unmatched — neither is a reason to override the publisher.

### Köppen group is absent from S3 runs

`DEFER`

No bucket column corresponds to `Koppen Group (as of 2024)`. An S3 run therefore writes
`koppen = None` for every gauge and the page's Köppen grouping is empty; a `--gauge-source
local` run still picks it up from the xlsx as before.

Filling it from the xlsx was implemented and then removed, deliberately: it was the same
mechanism as the reach-match override above, and keeping it meant keeping a code path whose
whole purpose was to disagree with the publisher. Unlike reach matching this one is harmless —
Köppen is a label, not an identity — but it is not currently wanted.

Deriving it from latitude/longitude against a published Köppen–Geiger raster would give every
run the grouping with no local file at all. Not tried.

### How this was nearly missed

Presence is not the test. Every one of the 2,626 gauges in the VPU 714 baseline *appears* in
the bucket catalog, and checking only that is what produced the first, wrong conclusion. The
test is whether `final_river_id > 0` in each catalog for the same gauge — and then, before
treating a difference as a gap, whether the "missing" gauge is the same station under another
name.


---

## Not an issue, recorded for reference

- **Stream order and drainage area are available** in `tables/v2-model-table.parquet`
  (`strmOrder`, `USContArea`, plus `DSLINKNO` topology). `LINKNO` joins to the catalog's
  `final_river_id` with a **100% match rate** on VPU 714 (2,632/2,632).
- **All sampled gauge series are daily** (median timestep 1 day, 60/60 sampled), matching
  `daily.zarr` with no resampling needed.
- **9,887 catalog gauges** are flagged `Higher temporal resolution available?` — the path to an
  hourly evaluation later.
- **Model output is and will remain deterministic**, so RPS, CRPS, Brier score, reliability
  diagrams, rank histograms and ROC/AUC are permanently out of scope. Threshold-based
  contingency metrics (CSI, POD, FAR, ETS) remain fully applicable.
- **SABER / locally-applied bias correction** is out of scope for now; the version-comparison
  machinery (paired per-gauge differences, change maps, improved-vs-degraded counts) will
  handle it when it arrives.

---

## A note on gauge identifiers in this file

**Individual gauges are not named here, and observed data of any kind stays out of this
repository.** The observations are licensed by the agencies that collect them and are not ours
to redistribute, in raw or derived form.

What that rules out: station ids, coordinates, and any figure attached to a named station.
Earlier revisions of this file carried a handful of each; they were removed, and the findings
they illustrated are stated without them above.

What it does not rule out: statistics pooled across the whole run — medians, counts,
percentile tables, the share of gauges past a threshold. Those are the substance of an
evaluation and identify no station. If a future measurement only makes sense with an example
station attached, describe the pattern instead, as the CARAVAN prefix note does.

`.gitignore` enforces the file side of this — `*.csv`, `*.parquet`, `*.xlsx`, `*.npz`,
`routing/` and any built `*_explorer.html`, which inlines every gauge's flow-duration curve
and monthly regime. The only HTML that is source is `webapp/explorer.html`, the template.
