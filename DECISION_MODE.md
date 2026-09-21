# Decision mode: what RFS could be informing from the retrospective

Brainstorm, not decided. Written 2026-09-17. This is the candidate list of real decisions an
RFS user makes using retrospective data, so that a second evaluation mode can score the model
on *whether it informs the decision correctly* rather than on how closely its hydrograph
matches a gauge.

Most of this is not committed. The reference ("what counts as chance") is deliberately left open
per decision — see [Open questions](#open-questions). Decision 1 is being built first; its
settled choices are in [Decision log](#decision-log).

## Decision log

Recorded so they can be revisited. Each entry says who chose it and what changes if it flips.

| Date | Choice | Made by | If it changes |
|---|---|---|---|
| 2026-09-17 | Build decision 1 (HydroSOS low flow) first | user | — |
| 2026-09-17 | Reference period = **per-gauge model/gauge overlap**, not fixed 1991–2020 | user | re-derive every percentile bin; gauge set changes |
| 2026-09-17 | Minimum record **more than 10 years** — the paper's rule | user | gauge set changes; ~154 gauges on this VPU |
| 2026-09-17 | "Covers N years" measured as **count of valid months after the 50% rule** | user | gauge set changes; gappy records fall out |
| 2026-09-17 | Categorisation method and thresholds from current `statuscalc.py` | user | all categories change |
| 2026-09-17 | Score **both** "dry" (≤25th) and "extremely dry" (≤10th) | user | — |
| 2026-09-18 | Decision mode shows **only verdicts** — no statistics, no metric dropdown | user | removes the 10 `hs_*` picker entries |
| 2026-09-18 | Verdicts are a **traffic light**: yes / maybe / no, green / amber / red | user | — |
| 2026-09-18 | Gauges with insufficient record are **grey and labelled**, not hidden | user | adds a 4th state; see the grey table below |
| 2026-09-18 | The grey label reads **"not enough data to judge"** | user | — |
| 2026-09-18 | Thresholds must be checked across **several VPUs**, not tuned on 714 | user | 714 turns out to sit mid-pack, so it was not flattering itself |
| 2026-09-18 | Scored on the **extremely-dry band only** (category 1, driest 10%) | user | — |
| 2026-09-18 | **Red = luck cannot be ruled out**, per gauge, exact binomial, one-sided p ≤ 0.05 | user | the only threshold here that is derived rather than chosen |
| 2026-09-18 | **Green ≥ 0.50** catch, **Good 0.33–0.50**, **Weak** below | user | `VERDICT_GREEN` / `VERDICT_GOOD` in `kge_map.py` |

### Built 2026-09-18 — results on VPU 714

`python kge_map.py --vpu 714 --mode decision` then `serve.py`. Of 2,626 gauges:

| verdict | gauges | |
|---|---|---|
| Yes — catches at least half | 201 | 7.7% |
| Mostly — a third to a half | 1,083 | 41.2% |
| Weakly — beats luck, under a third | 913 | 34.8% |
| No — no better than luck | 346 | 13.2% |
| Can't say — not enough data | 83 | 3.2% |

Worked example of why red is a test and not a threshold: gauge 6657000 has 96 severe
months and caught 13, a catch rate of 0.135 — **above** the 0.10 chance rate, and it would pass
any fixed cut placed near chance. But luck alone matches 13 or more 16% of the time, so it is
red. Gauge 7331000 caught 49 of 96 and is green at p = 5e-24.

### Known objection to the 0.33 cut, recorded not resolved

0.33 is the **median** catch rate, so the good/weak boundary sits at the densest point of the
distribution. **71–80% of gauges have a 95% interval spanning 0.33**, meaning for three gauges
in four the record cannot say which side they belong on. Two neighbouring gauges can take
different colours from which severe months happened to fall in their records, and a reader will
see a spatial pattern that is not there.

The user was shown this and chose 0.33 anyway. Green (0.50) and red (the luck test) do not have
this problem — both sit where gauges are sparse or where the answer is definite.

### Grey is not a rounding error

Share of attempted gauges with no verdict at all — fewer than 11 usable instances of some
calendar month, so the breakpoints cannot be placed:

| VPU | grey | attempted | |
|---|---|---|---|
| 122 | 52 | 54 | **96.3%** |
| 208 | 366 | 1,386 | 26.4% |
| 209 | 347 | 1,852 | 18.7% |
| 714 | 83 | 2,626 | 3.2% |
| 712 | 28 | 974 | 2.9% |
| 609 | 3 | 273 | 1.1% |

This is the strongest argument for showing grey rather than hiding it. On VPU 122 the map is
**almost entirely grey** — the honest message being that nothing can be said there. Hiding those
gauges would leave two dots on an empty map and imply the region had been assessed.

**Superseded:** a 20-year minimum was chosen earlier on 2026-09-17 and reverted the same day to
the paper's >10 years. On this VPU the difference is ~154 gauges (2,582 vs 2,428 of 2,626) — the
median record here is 58.7 years, so the rule barely binds. It would bind hard in a data-poor
VPU, which is what it was written for.

### Inclusion rules from the Nile Basin paper

The user's published HydroSOS/GEOGLOWS work used these. Quoted as given:

1. Record must cover **more than 10 years**, to establish monthly categories.
2. **Any month including less than 50% of the values was excluded** — standard HydroSOS protocol.
3. Stations had to be **correctly matched to an RFS stream**.
4. **At least some data in 1990–2020**, since that is the 30-year period used to obtain the
   long-term average.

Rule 2 settles month completeness: **≥50% of days**. Note this is *not* the same quantity as
`MIN_DAYS_PER_MONTH = 60` in `kge_map.py`, which counts paired days accumulated across all years
for one calendar month. Different measure; do not conflate them.

Rule 1 also implies no separate per-calendar-month floor — the paper governs by overall record
length, not by n per month.

### Where this departs from the paper, and why

| | Paper | Here |
|---|---|---|
| Record length | more than 10 years | same |
| Reference period | 1990–2020 | **per-gauge model/gauge overlap** |

The reference period is the one real departure. In `statuscalc.py` the reference window filters
the data used for **both** the long-term average and the percentile ranks, so it decides where
the breakpoints come from — it is not only a normalisation detail.

The reason for changing it: the model has complete coverage 1940–present and the gauges do not.
Under a fixed window the model's bins would rest on 30 full years while each gauge's bins rest on
whatever it has, so the two series would be ranked on different sample sizes. Under the overlap
both rest on exactly the same months, which is what an *agreement* test needs. The cost is that
the status product itself is no longer comparable between gauges, or with the paper.

### The method comes from current `statuscalc.py`

**Decided 2026-09-17 (user):** inclusion rules from the paper, but the categorisation method and
thresholds from the current `wmo-im/HydroSOS` `status/statuscalc.py` — some thresholds have
changed since the paper's script. Read from source, not from the README:

| Step | What it does |
|---|---|
| 1 | Monthly mean of daily flow; **set to NA if the month is <50% complete** (`monthly%` < 50) |
| 2 | Long-term average per calendar month over the **reference window**; each monthly mean becomes `mean_flow / LTA[month] * 100` |
| 3 | Weibull rank `rank/(count+1)` **within the reference window only**, per calendar month; breakpoints at 0.10, 0.25, 0.75, 0.90 by linear interpolation between bracketing ranks |
| 4 | Category 1–5: `≤p10 → 1`, `≤p25 → 2`, `≤p75 → 3`, `≤p90 → 4`, `>p90 → 5` |

So **category 1 is "extremely dry" and category 2 is "dry"** — the two this evaluation scores.

Two structural points that are easy to miss:

- **The reference window and the classification window are separate.** Breakpoints come only from
  the reference window; categories are then assigned to *every* month in the record. A month
  outside the reference window still gets a category.
- **A gauge is dropped outright if any calendar month is absent from the LTA** — `statuscalc.py`
  prints `Month %i missing in Long Term Average` and skips the file. All 12 calendar months need
  at least one valid monthly mean inside the reference window. That is a harder filter than the
  paper's rule 4 ("at least some data in 1990–2020") and will remove gauges that rule 4 keeps.

### Settled spec for decision 1

Per gauge, model and observed series handled **independently and identically**:

1. Daily → monthly mean. A month with **<50% of days present is NA**.
2. Reference window = the **model/gauge overlap** for that gauge.
3. Require **>10 valid instances of every calendar month** inside the window, else drop the gauge.
4. `LTA[month]` over the window; each monthly mean becomes `mean_flow / LTA[month] * 100`.
5. Weibull rank `k/(n+1)` per calendar month within the window; breakpoints at 0.10, 0.25, 0.75,
   0.90 by linear interpolation between bracketing ranks.
6. Category 1–5 per `flow_status()`.

Then compare the two category series on months where both are non-NA:

- **"Dry or worse"** — category ≤ 2. 2×2 contingency; reuse the existing `pod`/`far`/`csi`/`ets`
  path. `ets` is already chance-corrected, so its sign is the stoplight.
- **"Extremely dry"** — category = 1. Same machinery, stricter band.
- **Full 5×5** — weighted kappa, since the categories are ordered and confusing 3 with 1 is a
  worse error than confusing 2 with 1.

Chance agreement for the 5-category case is fixed by construction at
`0.10² + 0.15² + 0.50² + 0.15² + 0.10² = 0.315`, so kappa's denominator is exact rather than
estimated.

**One interpretation, flag if wrong:** rule 3 reads ">10 years" as *>10 valid instances per
calendar month*, not *>120 valid months in total*. The paper's own justification — "to establish
monthly categories" — points that way, and it is what the per-calendar-month ranking actually
needs. The looser reading would admit gauges whose Januaries are too sparse to place a
breakpoint.

### First run on VPU 714, 2026-09-17

Full record 1940-01-01 .. 2026-09-09, local gauges. **2,543 of 2,626 gauges scored**; 83 dropped
for not having more than 10 usable instances of every calendar month. The scarcest surviving
month has exactly 11, so that filter binds where it was meant to.

| | p05 | p50 | p95 | above 0 |
|---|---|---|---|---|
| kappa, weighted | +0.148 | +0.346 | +0.469 | 99.5% |
| kappa, unweighted | +0.056 | +0.186 | +0.298 | 99.1% |
| ETS, dry or worse | +0.070 | +0.212 | +0.340 | 98.8% |
| ETS, extremely dry | +0.024 | +0.150 | +0.305 | 98.0% |

**This is the soft-benchmark problem, measured.** The model beats chance essentially everywhere,
so a sign-based stoplight paints ~99% of the map green — while a weighted kappa of 0.35 is only
"fair" on the conventional Landis–Koch scale and the unweighted 0.19 is "slight". The verdict
"better than chance" is true and nearly uninformative. This is the concrete argument for
replacing chance with a harder reference, and it is the first real evidence the project has for
it.

## Decision 2 — UNPINNED 2026-09-18. Timing tolerance rescues it

The pin below was correct about the annual-maximum framing and wrong about the decision. The
user proposed scoring a hit when the model floods **within 3 days** of an observed flood, on
declustered daily exceedances rather than annual maxima. Measured on 286 gauges, VPU 714,
7-day declustering:

| | events/gauge | hit rate | chance | Cochran Q | real share of spread |
|---|---|---|---|---|---|
| T = 2 | 47 | 0.276 | 0.024 | p = 1.1e-53 | **63%** |
| T = 10 | 7 | 0.125 | 0.003 | p = 0.0027 | 21% |

Against **10% at T=2 and 0% at T=10** for the annual-maximum version. Two changes did it:
counting every declustered exceedance gives 47 events a gauge instead of 6 annual maxima, and
the window stops routing lag being scored as error. Skill above chance more than doubles from
same-day (0.122) to ±3 days (0.265), while chance stays at 0.024 — the gain is not the wider
window letting luck in.

At T=10 same-day matching gives the median gauge **literally zero hits**; ±3 days gives 0.125.

Undecided: the declustering separation (7 days is mine, not agreed), which return periods to
show, and where the verdict boundaries go.

## Decision 2 — the earlier analysis that pinned it

Settled design: classify each **year's annual maximum** into a return-period band, each series
against thresholds fitted from its **own** record (Gumbel MoM, as RFS does), so volume bias
divides out. Annual maxima rather than days because a flood spans ~2.9 days and daily counts
break independence.

It works. Band shares land where the return-period definitions predict and the two series match
each other — gauge 53.8/28.1/8.6/5.2/4.3%, model 52.9/29.0/8.4/5.2/4.6%. Chance agreement 0.378,
actual 0.488, kappa **0.178**, and 82% of years land within one band. Comparable to decision 1's
0.186.

**Why it is pinned:** it cannot support a per-gauge verdict.

- To demonstrate agreement of 0.488 against chance 0.378 takes about **145 years** of paired
  record at 95% confidence and 80% power. **Zero gauges in six VPUs have even 100 years**, and
  the retrospective starts in 1940, so 86 years is the hard ceiling.
- Every alternative slicing lands between 49% and 77% red: 5-band 48.6%, binary at the 2-year
  level 54.2%, 10yr+ only 77.3%. Making the question easier does not help, because chance gets
  easier too.
- A per-gauge CSI map does not escape it. Cochran Q on between-gauge variation: at the 2-year
  level real differences exist but are **~10% of the visible spread** (p = 0.019); at the
  10-year level they are **indistinguishable from noise** (p = 0.72, observed variance 0.0369
  against 0.0396 expected from sampling alone). A 10-year CSI map would show spatial pattern
  that is not there.

**What is solidly established, pooled:** the model does have real flood skill — kappa 0.178,
82% within one band, catch 0.64 against 0.47 chance at the 2-year level, over 165,000
gauge-years. What cannot be established is whether it is better at one river than the next.

Options left open: pool to a regional statement; per-gauge at the 2-year level as a score with
the noise share declared; or drop it.

## Wet and dry days — built 2026-09-21. BANDS ARE PROVISIONAL

*"Does the model correctly identify which days are wet and dry?"* — the user's reframing after
the trend question ran into record-length limits. Scored from the **`spearman` column the metric
table already carries**: daily rank correlation between modelled and observed flow. No new
computation, no re-run; `serve.py` derives the verdict at display time, so retuning a band takes
effect on the next page load.

| band | VPU 714 |
|---|---|
| Strong ≥ 0.70 | 11.2% |
| Good 0.50–0.70 | 55.4% |
| Weak 0.30–0.50 | 24.6% |
| Poor < 0.30 | 8.8% |

**Why this one behaves where the others did not:** it scores **100% of gauges in every VPU**, and
the estimate rests on ~20,000 daily pairs rather than 50 annual values. Standard error is 0.012
at 20 years and still 0.05 with a single year of record. It never runs out of evidence, which is
exactly what defeated decisions 2 and 3.

### COME BACK AND ADJUST THESE

The four cuts — 0.70 / 0.50 / 0.30 — were read off the VPU 714 distribution (p10 0.32, median
0.57, p90 0.71) and are **anchored to nothing**. They are the same kind of chosen-by-eye number
the user objected to over the kappa bands, and they should be revisited. Constants are
`WETDRY_STRONG` / `WETDRY_GOOD` / `WETDRY_WEAK` in `kge_map.py`.

**The measured alternative, offered and not taken.** A model that knows only the calendar —
predicting the day-of-year average every year, with no weather in it — scores a median **0.506**
against the real model's 0.569. The real model beats calendar-only at just **65% of gauges**. So
most of the headline number is the model knowing it is spring.

That benchmark is computable per gauge and varies with how seasonal the river is, which would
make the bottom band mean *"no better than the seasonal average alone"* — derived rather than
chosen, in the way decision 1's red is. Stripping the calendar out leaves a genuine median
**0.469** on anomalies.

The user was shown all of this and accepted seasonality driving the score: for someone asking
when a river runs high or low, getting the seasonal timing right is part of the answer. Recorded
because the choice is revisitable, not because it was wrong.

## Decision 3 — wetting or drying. First measurement 2026-09-18

Mann-Kendall on annual mean flow, identical window for both series, years needing ≥300 paired
days, p < 0.05. 277 gauges on VPU 714, median 61 years.

| | gauge says | model says |
|---|---|---|
| wetting | 30.0% | 9.0% |
| no trend | 64.6% | 79.8% |
| drying | 5.4% | 11.2% |

Agreement 0.567, chance 0.549, **kappa 0.040** — essentially no skill.

The cross-tab is the finding. Of 83 gauges the observations call **wetting**, the model calls
wetting at **13**, no trend at 66, and drying at 4. Of 15 called drying, the model agrees at 2.
**When a river is genuinely trending, the model gets the direction right about 15% of the time**,
against ~9-11% by chance given its own marginals.

The marginals disagree as much as the pairings do: the gauges show a strong wetting signal in
this basin (30% wetting against 5% drying) and the model shows no such thing (9% against 11%).

Candidate causes, none tested: ERA5 precipitation trends not matching observed; land-use change,
irrigation return flow and regulation altering observed streamflow without a climate signal the
model could see; or inhomogeneity in the gauge records themselves. **Caveat:** plain
Mann-Kendall over-rejects on autocorrelated series, so both trend counts are likely inflated —
this affects both series, but it has not been checked with prewhitening.

## Still open

- **What "correctly matched to an RFS stream" meant operationally.** If it was
  `final_river_id > 0`, that filter is already applied here. If there was further QC — drainage
  area agreement, manual review — it is not implemented and would change the gauge set. See
  KNOWN_ISSUES 1 and 2 for the known matching problems on this VPU.
- **Where this file belongs.** It is a new file; the project convention is that undecided things
  live in `KNOWN_ISSUES.md`. Folding it in has not been decided.

### Consequences of the overlap choice

Each gauge's bins come from its own window, so **status is not comparable between gauges** —
"March 2005 was extremely dry" means something different at two gauges with different overlaps.
The agreement score per gauge is unaffected, and kappa is normalised, so verdicts remain
comparable. Only the underlying status product is not.

HydroSOS suggests 30 years and the rule here is >10, so the low breakpoint can rest on very few
values. Weibull ranks are `k/(n+1)`, so at n = 11 the 0.10 target falls between the 1st and 2nd
smallest values — the "extremely dry" threshold is then set almost entirely by the single lowest
month on record. Category 1 agreement will be noisier at short records than category 2, even
though both gauges passed the same filter. Worth carrying `n` per calendar month into the output
so this is visible rather than assumed.

## The intended output

Per gauge, per decision, one of:

| | meaning |
|---|---|
| **green** | beats the reference, and the record is long enough to say so |
| **yellow** | indistinguishable from the reference |
| **red** | worse than the reference |
| **grey** | too few events in the paired record to rule either way |

Grey is not a rounding of yellow. "The model adds nothing" and "we cannot tell" are different
answers and a user acts differently on each. Which of the four applies is a property of the
*gauge and the decision together*, so grey coverage will differ per decision — driven by record
length, not by model quality.

## How much the decision demands of the model

Decisions differ in what has to be right, not just in how right it has to be. Roughly ascending:

1. **Within-reach ranking** — invariant to any monotone transform of flow. Bias, scale error and
   wrong units all cancel. Most forgiving.
2. **Cross-reach ranking** — survives spatially uniform bias, fails on spatially structured bias.
3. **Distribution shape** — scale-invariant, shape-sensitive.
4. **Magnitude** — location, scale and tail all have to be right. Nothing cancels.
5. **Trend** — forgives constant bias, demands temporal homogeneity of the forcing. ERA5 has
   known inhomogeneities from the changing observing system, so this is its own hazard rather
   than simply the hardest rung.

The ladder predicts the result before it is computed: the same reach should come out green at
rung 1 and red at rung 4. That contrast is arguably the point of the mode.

## The catalog

`Rung` is the list above. `Have it?` is what exists in the metrics parquet today.

| # | Decision, as the user asks it | Reads | Rung | Have it? |
|---|---|---|---|---|
| 1 | Is the forecast peak a notable flood? | return-period bands | 1 | **yes** — `ets`/`csi`/`pod`/`far` at 2 yr |
| 2 | Which return period should be my action trigger? | return-period bands | 1 | partly — 2 yr only |
| 3 | How big is the 25/50/100-year flood here? | return-period table | 4 | no |
| 4 | Is today's flow unusual for this time of year? | day-of-year climatology | 3 | close — `ss_clim` is the same reference |
| 5 | Is this season shaping up wet or dry? | seasonal distribution | 3 | partly — `ss_clim_m<MM>` |
| 6 | Is there reliably enough water for this withdrawal? | flow-duration curve | 3 | no |
| 7 | Is the minimum environmental flow being met? | low-flow threshold | 3 | no |
| 8 | When is the wet season here? | monthly climatology | 1 | partly — monthly block |
| 9 | Which of these reaches is most flood-prone? | return periods, cross-reach | 2 | no |
| 10 | Where should the next gauge go? | any, cross-reach | 2 | no |
| 11 | Is this reach flashy or steady? | full series | 3 | no |
| 12 | Should I trust the model on this reach at all? | everything | — | this tool |
| 13 | Should I bias-correct against a nearby gauge? | full series | 3 | partly — see note |
| 14 | Has flooding here got worse over the record? | annual maxima | 5 | no |
| 15 | Are we in streamflow drought right now? | low-flow percentiles | 3 | no |

### Notes on the ones that need them

**1 — Is the forecast peak a notable flood?** The core RFS use of retrospective data, and the
one already scored. Worth stating plainly what it does and does not test: because each series is
compared to *its own* 2-year level (KNOWN_ISSUES P), it asks "on the days the gauge calls a
2-year flood, does the model also call one?" That is exactly the operational question, since RFS
thresholds are themselves derived from the retrospective — but it is a timing-and-ranking test,
not a magnitude test. Decision 3 is the magnitude counterpart and must be labelled as a
different thing or the two will be conflated.

**2 — Trigger selection.** Same machinery as 1, repeated at 5/10/25 yr. Expect the grey region
to grow fast with return period: a 25-year level on a 60-year paired record gives two or three
events, which cannot distinguish skill from luck. That growth is itself the finding.

**3 — Design flood magnitude.** Rung 4, and the honest answer for the long return periods is
that it is **unscoreable by construction** — no gauge record is long enough to validate a
100-year estimate, and none ever will be. People want this one more than almost anything else on
the list. Saying "we cannot tell you, and neither can anyone else" is a legitimate output and a
better one than a score built on two events.

**4 and 5 — Anomaly and season.** `ss_clim` already scores against a leave-one-year-out
day-of-year climatology, which is the same reference these decisions imply. The gap is framing,
not computation. Caveat from KNOWN_ISSUES H applies: that reference is built from the target
gauge's own record, so it supports "the model carries more information than the season alone"
and not "the model beats what you'd have at an ungauged reach."

**6, 7, 15 — The dry end.** All three read the lower tail, where the model behaves differently
than it does on peaks and where the zero-flow caveat bites: per KNOWN_ISSUES F, RFS routing
essentially never outputs zero, so any dry/wet contingency score measures a known structural
absence rather than performance. Thresholds for these should sit at a percentile that the model
can actually cross, not at zero.

**9, 10 — Cross-reach.** Rung 2 and unusually robust to what GEOGLOWS is worst at, since uniform
bias cancels out of a ranking. Needs a different scoring shape from everything else here —
rank correlation across gauges rather than a contingency table per gauge — so it will not drop
into the same per-gauge table. Possibly a separate view.

**13 — Bias correction.** There is an arithmetic result in KNOWN_ISSUES E that bears directly on
this: a monotone per-reach transform can move `beta` and `gamma` and cannot move `r` or Spearman.
So this decision is partly answerable from the existing metrics — if correlation is the dominant
error at a reach, correction will not rescue it. Whether correction raises KGE' by a useful
amount on this network has not been tested.

**14 — Trend.** Flagged for completeness and carrying the loudest caveat: ERA5 forcing
inhomogeneity means a trend agreement or disagreement may say more about the reanalysis than
about the model. Low priority.

## Open questions

Deliberately not resolved yet.

- **What is the reference for each decision.** Nearest-donor transfer is declined
  (KNOWN_ISSUES H). Remaining candidates: gauge climatology, the marginal-frequency correction
  already inside `ets`, v1 of the model, or the decision the user would reach from the forecast
  alone. It need not be the same choice for every row.
- **How yellow is defined.** Proposal: bootstrap CI on the skill score straddling zero, rather
  than an arbitrary band. Untested.
- **Direction of error.** The stoplight compresses away whether the model runs high or low.
  Under-predicting a flood and over-predicting it are not the same mistake. May need a glyph
  or hatch carrying sign alongside the colour.

  **Measured 2026-09-17, and for decision 1 the answer is that it cannot be recovered.** Both
  series take their categories from their own record, so each flags 25% of months as "dry or
  worse" and 10% as "extremely dry" by construction. `hs_dry_freq_bias` on VPU 714 comes out at
  1.000 median, p05 0.992 — pinned at 1, carrying no information. The property that makes this
  test immune to volume bias is the same property that makes it blind to direction. The columns
  are written to the parquet but left out of the map picker. A decision that needs direction
  needs absolute thresholds, which is a different test.
- **Asymmetric loss.** "Better than chance" weights misses and false alarms roughly equally and
  no flood decision does. Options: a cost-loss slider, or reporting the range of cost-loss
  ratios over which the model has positive value.
- **Whether the decision verdicts track the metrics.** If KGE' turns out to predict these
  verdicts poorly — likely at rungs 1 and 2, where bias cancels but KGE' still punishes it —
  that is both the justification for the mode and a result worth reporting. Mode one becomes the
  control rather than the thing being replaced.
