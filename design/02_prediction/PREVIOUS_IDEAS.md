# Prediction — previous ideas (not built)

> **Status: exploration, not the design.** What `aqm predict` actually does today is in `DESIGN.md` next to this file — a two-line extrapolation of the recent rate. This document records the eight forecasting approaches considered for replacing it, each judged on its own against the requirements of §1.2, with its own independent drawbacks and no preferred option. Any of them would write the same prediction artifact (`DESIGN.md` §1), so adopting one changes no other stage.

Candidate designs for the `organic_usage_predictor`, the component that forecasts the user's organic (interactive) usage for the planner of `../DESIGN_v1.md` §2. It runs every 30 minutes, once per agent (Claude, Codex). This file records every approach considered, with its strengths and its drawbacks, each approach judged on its own against the requirements of §1.2.

## 1. What the predictor must produce

The system ultimately decides on two quantities: the quota remaining at the end of the current 5-hour window, and the quota remaining at the end of the 7-day period. The predictor supplies the organic half of both; the planner adds the extra work and the 5-hour caps, because how much of the user's demand can actually be spent depends on the extra work placed in each window.

| Output | Used by | Precision needed |
|---|---|---|
| Distribution of `demand_5h`: organic demand from now until the end of the current 5-hour window, and P(the window saturates) | Window guard: how much extra work fits now without blocking the user | High |
| Distribution of `demand_7d`: organic demand from now until the end of the 7-day period | Planner: `reserve` and `extra` | Low beyond a day or two |
| `activity_probability(t)` for every future slot | Planner: which slots are cheap for extra work | Medium |

Whether these outputs are needed at all depends on the scheduling policy: a fixed nightly top-up needs none of them, and an away-day signal alone may capture most of the gain. See `../03_budgeting/PREVIOUS_IDEAS.md`.

**Demand, not spend.** The predictor forecasts what the user *would* spend with no limits. Converting demand into spend (`min(demand, cap left in the window)`) is done by the planner. This also makes saturation predictable: a user saturating the current window is predicted to saturate the next one too.

**No window open.** When no 5-hour window is running, the window guard needs the demand of the window that the extra work would open, i.e. over the next 5 hours.

### 1.1 Why the 7-day horizon is needed

A tempting simplification is to predict only until the end of the current 5-hour window and assume every later window can be filled. That is the waste-free floor (`../CONSIDERATIONS.md` §1–2): correct as a bound, but as a planning rule it acts too late and forces extra work into the user's working hours.

Example (Claude, reset Monday 21:00, 5.5 units left on Saturday 10:00):

- **Floor only:** more future windows than quota left, so nothing runs on Saturday or Sunday night. The floor binds 27.5 hours before the reset (Sunday 17:30), and from then every window must be saturated until Monday 21:00, blocking the user all Monday. Any missed window becomes waste.
- **7-day view:** extra work runs on Saturday night (2 units) and Sunday night (2 units), and Monday starts with 1.5 units for the user.

The horizons do not need the same precision:

| Horizon | Enough |
|---|---|
| Current 5-hour window | Current state of the user (session, pace) and correlation between consecutive slots |
| Rest of today and tonight | Recent information fading into the seasonal profile |
| Further days until the reset | The seasonal profile only, per day or per window |

### 1.2 Requirements every approach is judged against

| # | Requirement | Why it matters |
|---|---|---|
| R1 | Produces the three outputs of §1, including a right tail (p95) for `demand_5h` | The window guard protects the user with the p95 |
| R2 | Nights stay at zero | Nights are where extra work runs |
| R3 | Session continuity: an ongoing session continues (even at an unusual hour), a stopped one stops | Dominates `demand_5h` |
| R4 | Correlation between slots within a window: the spread of a sum is realistic | Independent slots make the p95 of a sum too low (§3.1) |
| R5 | Day-level correlation and regimes: away days, heavy weeks | Dominates `demand_7d` |
| R6 | Demand, not spend: capped slots do not lower the forecast | Heavy days are exactly the capped ones |
| R7 | Works with about 4 weeks of history and few parameters | The data available |
| R8 | Adapts to usage heavier than ever observed | New projects, new habits |
| R9 | Explainable and cheap to compute every 30 minutes | Debugging and trust |

## 2. Terminology and data

| Term | Definition | Example |
|---|---|---|
| Unit | The quota of one full 5-hour window (Claude Pro ≈ $32, Codex Plus ≈ $20.5) | |
| `t` | A slot: the 30-minute interval starting at `t`; slots start at :00 and :30 | `t` = Tuesday 2026-09-22 14:00 is 14:00–14:30 on that date |
| `t0` | The current slot | |
| `end_5h`, `end_7d` | End of the current 5-hour window, end of the current 7-day period | |
| `week_bucket(t)` | Same weekday and time of day as `t`, in every past week | "any Tuesday, 14:00–14:30" |
| `day_bucket(t)` | Same time of day as `t`, on every past day | "any day, 14:00–14:30" |
| `spend(t)` | Organic spend in past slot `t`, in units | |
| `pace(t)` | The user's recent spend rate just before `t` (e.g. spend over the last hour) | |
| Active slot | At least one organic message in the slot | |
| Blocked slot | The 5-hour meter was at 100% during the slot: the user could not work | |
| Inactive slot | Neither active nor blocked | |
| Engaged day | A day of normal working life (nights and breaks included) | |
| Away day | A day with no work at all (vacation, several days off) | |
| `recent_*` | Anything computed from short-term past data | |

### 2.1 Data preparation common to all approaches

- **Organic only:** sessions started by this system are tagged and excluded.
- **Spend per slot:** per-message spend from local session files, converted to units with the meter weights of `CONCLUSIONS.md` (list-price USD, cache reads weighing less), and assigned to the slot of the message timestamp.
- **Local time:** slots are aligned on local clock time (Europe/Paris), so summer/winter time changes do not shift the daily profile.
- **Blocked slots:** detected from the quota history (5-hour meter at 100%). Each approach handles them in its own way (excluded, skipped or imputed); this is listed in its drawbacks.
- **Current slot:** `t0` is in progress; whether its partial spend counts as an observation is to be decided (§17).

## 3. Approach A — Active/inactive Markov chain

The user is active or inactive in each slot, and the probability of switching depends on the hour of the week: `p_start(bucket)` = P(active next slot | inactive now), `p_stay(bucket)` = P(active next slot | active now). Spend of an active slot is drawn from a per-bucket distribution, multiplied by a recent intensity factor that decays back to 1. The forecast starts from the current state and propagates slot by slot until the reset.

Computation: originally N = 1000 simulated futures. Simulation is not required: `P(active)` per slot comes from forward recursion of the state probabilities, and the distribution of any sum from dynamic programming over (state, cumulative demand on a grid), deterministically and in milliseconds (§13).

**Strengths:** one mechanism for all horizons (the chain starts from the current state and forgets it, converging to the weekly profile); session continuity and correlation within a window are built in; P(saturation) is natural.

### 3.1 The independence problem (reference example)

This example is used throughout the file. Take 10 afternoon slots, each active with probability 0.5 and costing 0.1 unit when active.

| Model | Mean | p90 | P(whole afternoon worked) |
|---|---|---|---|
| Independent slots (10 coin flips) | 0.5 | 0.7 | 0.1% |
| Real sessions (one coin flip for the afternoon) | 0.5 | 1.0 | 50% |

Same mean, much narrower spread. A model that treats slots as independent keeps too little room for the user in the window guard, assumes "half of every day" instead of "some full days, some empty ones" for the 7-day reserve, and underestimates the probability of zero usage (waste risk). Approach A avoids it at the slot level, since P(active at `t+1`) depends on the state at `t`.

### 3.2 Drawbacks

- **Too many quantities for the data:** 2 × 336 transition probabilities and 336 spend distributions, from about 4 examples per bucket. Rare transitions (starting to work at 03:00, stopping at 15:00 on a Sunday) are estimated from 0–2 events; pooling across buckets is needed and not specified.
- **One slot of memory:** the chain does not know how long the session has lasted or how much the user already worked today, so session lengths are memoryless (a 10-minute session and a 6-hour one are equally likely to stop next slot).
- **Binary activity:** one tiny message makes a slot as "active" as an intense one; the spend of consecutive active slots is drawn independently, so a heavy session is not expected to stay heavy.
- **No day-level state or recent activity factor:** an away user is still expected to start working at the usual hours (R5 fails); only the intensity has a recent factor.
- **Blocked slots:** the state during a blocked slot is unknown, so transitions through it must be skipped, and heavy sessions are under-represented in the transitions.
- **Explainability:** harder to explain than a profile; outputs come out of a recursion rather than a readable formula.
- **Simulation noise** if simulated rather than computed by recursion.

## 4. Approach B — SARIMA with weekly seasonality

A single time-series model over 30-minute spend, with seasonal period 336 (one week).

**Strengths:** standard; short and long term in one model; analytic intervals, including for sums; the mean per slot and the variance of a total account for correlation between slots through the error model; can extrapolate to levels never observed.

**Kept as a benchmark:** harmonic regression (a few Fourier terms for daily and weekly cycles) with ARIMA errors on `log1p(hourly spend)`, in the backtest. Its Gaussian shape is acceptable for `demand_7d` (a sum of about 300 slots), where it could serve as a first version of the reserve.

### 4.1 Drawbacks

- **Seasonal period not estimable:** seasonal differencing at lag 336 uses up a whole week and forecasts "last week's value + noise" from a single noisy example; seasonal AR/MA terms at lag 336 cannot be estimated from 3–4 cycles.
- **Gaussian, symmetric noise:** the data is mostly zeros plus bursts; intervals `mean ± 1.645σ` give negative lower bounds, non-zero night spend and a poor right tail. Wrong exactly for `demand_5h` (few slots, skewed total, p95 needed).
- **Same variance at every hour:** nights get uncertainty they do not have, afternoons too little.
- **Linear dynamics:** "next = a × current + noise" cannot represent "the session either continues or stops" (two outcomes); the mean is right, the shape is wrong.
- **No activity probability:** it forecasts a mean spend, not P(active), which the planner needs for slot costs.
- **Log transform:** `log1p` improves the shape, but sums must be computed back on the original scale, which ARIMA does not give directly.
- **Capped slots:** no support for censored observations.
- **Harmonic variant:** a few Fourier terms smooth out sharp features (the abrupt start of the working day), and the Gaussian problems remain.

## 5. Approach C — Hierarchical averaging with a recent scaling factor

Proposed by the user: a daily profile (48 buckets) as the base prior, refined by weekday profiles (7 × 48 buckets, unstable), the two averaged; a recent "current scaling factor" (recent observed / expected, exponentially weighted) applied to the near future with the same exponential decay.

**Strengths:** simple, explainable, few parameters, robust with little data; the multiplicative factor keeps nights at zero.

### 5.1 Drawbacks (as originally proposed)

- **Means only:** profiles of averages give no quantiles, while the planner needs risk (p90, p95).
- **Fixed 50/50 blend** of the daily and weekday profiles, whatever the amount of data behind each.
- **Unsmoothed ratio:** explodes when the expected spend is about 0 (nights, short samples).
- **One factor for two effects:** "user present" and "user working intensely" are scaled together.
- **Slots independent:** no session continuity and no correlation within a window (§3.1).
- **Capped slots** not handled.

### 5.2 Adjustments made during review

- Weight the two levels by their amount of data (`w = n / (n + k)`), which is hierarchical shrinkage with one parameter.
- Keep the probability of activity and the spend when active separately.
- Smooth the ratio `(observed + k) / (expected + k)`; skip blocked slots.

These adjustments led to Approach D (§6).

## 6. Approach D — Seasonal activity, non-seasonal intensity, two recent factors

Built on the observation that activity is strongly seasonal (no work at night) while the spend of an active slot has no obvious reason to depend on the hour or the weekday. Both are corrected by recent data, with one shared decay.

### 6.1 Components

| Component | Definition | Time scale |
|---|---|---|
| `activity_prior(t)` | P(slot `t` is active), seasonal: `w_season(t) × p_week(t) + (1 − w_season(t)) × p_day(t)`, where `p_week(t)` and `p_day(t)` are the active shares in `week_bucket(t)` and `day_bucket(t)`, and `w_season(t) = n_week(t) / (n_week(t) + k_season)` | Long-term history |
| `intensity_prior_distribution(i)` | Distribution of `spend` over past active, non-blocked slots; no seasonality, does not depend on `t` | Long-term history |
| `factor_recent_activity` | `(recent active slots + k) / (recent expected active slots + k)`, where expected = Σ `activity_prior` over the same recent slots | Recent, exponentially weighted |
| `factor_recent_intensity` | `(recent spend + k × mean_i) / (recent expected spend + k × mean_i)` over recent active, non-blocked slots, where `mean_i` is the mean of `intensity_prior_distribution` | Recent, exponentially weighted |
| `recent_vs_prior_halflife` | Hyper-parameter, fixed (e.g. 3 days): weight of recent slots when measuring the factors, and decay of the factors over the forecast | |

The two factors are ratios on purpose: a recent level blended directly into the forecast would predict night activity after an active day, while a ratio multiplies a night prior of 0 and keeps it at 0.

### 6.2 Forecast for each future slot `t`

```text
w(t)                    = 0.5 ^ ((t − t0) / recent_vs_prior_halflife)          # 1 now, 0 far ahead
activity_probability(t) = min(1, activity_prior(t) × ((1 − w(t)) + factor_recent_activity × w(t)))
m(t)                    = (1 − w(t)) + factor_recent_intensity × w(t)
demand(t)               = 0                                   with probability 1 − activity_probability(t)
                        = i × m(t),  i ~ intensity_prior_distribution        otherwise
```

`demand(t)` is a mixture: zero if the slot is inactive, otherwise an intensity drawn from the prior and scaled by `m(t)`. Sums are computed by dynamic programming (§13).

### 6.3 Behaviour on typical situations

| Situation | Effect |
|---|---|
| Thursday 03:00 | `activity_prior` ≈ 0, so `activity_probability` ≈ 0 whatever the factors |
| No activity for two working days | About 20 expected active slots, 0 observed: `factor_recent_activity` ≈ 4/24 ≈ 0.17, so near-future activity drops (user probably away) |
| Active since this morning | `factor_recent_activity` > 1, raising the rest of today |
| Saturating every window on a token-hungry task | `factor_recent_intensity` measured on pre-cap slots is high, so demand in the next window exceeds the cap and saturation is predicted |

**Strengths:** few parameters; nights stay at zero; regimes (away, busy) are detected by the recent factors; can extrapolate above observed intensity through `m(t)`; explainable.

### 6.4 Drawbacks

- **Slots treated as independent** (§3.1): the spread of `demand_5h` and `demand_7d` is too narrow. Fixes: calibration of the spread in the backtest, empirical totals over the same span, or a correlation model (the chain of §3 at slot level, the engaged/away days of §12 at day level).
- **No session continuity:** the factors are measured over days (3-day half-life), so "the user sent a message 5 minutes ago" barely moves the forecast for the next hour; the nowcast of `../DESIGN_v1.md` §2.2 is not captured.
- **A session at an unusual hour is predicted to stop immediately:** at 03:00 `activity_prior` ≈ 0, and a ratio times 0 stays 0, so the window guard gives no protection to a user working at night.
- **One half-life for two roles:** the same `recent_vs_prior_halflife` sets how far back the factors look and how fast they fade in the forecast, and serves both factors (a vacation and a token-hungry afternoon have different time scales).
- **Intensity assumed non-seasonal:** not verified on the data.
- **`m(t)` scales every draw:** a high `factor_recent_intensity` stretches the whole intensity distribution, including its tail, which can overstate the p95.
- **Blocked slots excluded:** removing them from the intensity prior and the factors drops the heaviest moments and biases intensity downward.
- **`week_bucket` sparsity:** about 4 observations per bucket, handled by shrinkage with `k_season`.

## 7. Approach E — Seasonal spend distribution scaled by one recent factor

A simplification of D proposed during review: no distinction between activity and intensity. Each slot has a seasonal prior distribution of spend (zeros included); one scalar `factor_recent` (recent observed spend / recent expected spend, exponentially weighted) scales the upcoming distributions, fading from `factor_recent` to 1 over the horizon.

```text
factor_recent = (Σ w_s · spend(s) + k) / (Σ w_s · E[prior(s)] + k)      # past slots, exponential weights
m(t)          = 1 + (factor_recent − 1) · w(t)                          # w: 1 now → 0 later
forecast(t)   = prior(t) with every spend value multiplied by m(t)
```

**Strengths:** very few parameters; no assumption on the seasonality of intensity; zeros stay zeros; "user away" (factor ≈ 0) empties the near future; easy to explain ("running at 2.3× your usual pace").

### 7.1 Drawbacks

- **Scales amounts, not the chance of activity:** prior slot "20% chance of 0.1 unit" (mean 0.02); user active now, truth ≈ "90% chance of 0.1" (mean 0.09); factor 4.5 gives "20% chance of 0.45". The mean is right, the shape is wrong, and the p95 of the slot is 0.45 instead of 0.1: the window guard becomes far too protective, exactly where it matters.
- **Cannot forecast activity where the prior is ≈ 0:** a session at 03:00 has a prior ≈ 0, and ≈ 0 × anything stays ≈ 0.
- **One factor for two effects:** presence (on/off, lasting hours) and intensity (heavy prompts) may persist differently.
- **Sensitive to single large messages:** spend is burstier than activity counts; needs smoothing and winsorizing.
- **Seasonal prior needs more data:** a full distribution per `week_bucket` from about 4 samples, so pooling is required.
- **Slots independent** (§3.1).
- **Blocked slots** must be excluded from the factor.

## 8. Approach F — Weight matrix over past observations

Proposed by the user. One mechanism for every horizon: the forecast for a future slot is a weighted set of past observations, and the weights move smoothly from "the last few hours" (near future) to "the same time of day and weekday in past weeks" (far future). Approaches D and E are special cases of this weighting.

### 8.1 The weight matrix

`W[h, l]`: weight given to the observation at lag `l` (slot `t0 − l`) when forecasting horizon `h` (slot `t0 + h`). Plotted as a heatmap with one row per horizon and one column per lag:

| Row | Shape |
|---|---|
| `h` = +1h | Very high weight on the last few slots, dropping quickly |
| `h` = +10h | In between |
| `h` = +72h | Seasonal: bell-shaped bumps around the same time of day on every past day, larger bumps on the same weekday, fading slowly over the weeks |

The matrix is fully defined by a few fixed hyper-parameters (chosen by hand or tuned by backtest), not estimated cell by cell:

```text
W[h, l]        ∝ w(h) × recent(l) + (1 − w(h)) × seasonal(h, l)

recent(l)      = a × 0.5 ^ (l / recent_short_halflife)          # last hours: current session
               + (1 − a) × 0.5 ^ (l / recent_long_halflife)     # last days: current regime (busy, away)

seasonal(h, l) = bell(time-of-day distance between slot t0 − l and slot t0 + h; width seasonal_width)
                 × bump_week (> 1)  if slot t0 − l has the same weekday as t0 + h
                 × 0.5 ^ (l / seasonal_halflife)

w(h)           = 0.5 ^ (h / recent_vs_prior_halflife)
```

Example of the bell: to forecast 14:00, past observations at 14:00 get the most weight, those at 13:00 and 15:00 less, those at 12:00 and 16:00 little. Blocked slots get weight 0, and every row is renormalised to sum to 1.

### 8.2 Reading a row as sample weights

A row is not a set of regression coefficients (that would give only a mean). It is a weighting of past observations: the forecast distribution of `demand(t0 + h)` is the past values `spend(t0 − l)`, each counted with weight `W[h, l]`. Any quantile is a weighted quantile, read off directly. `activity_probability(t0 + h)` is the weighted share of active past slots.

**Strengths:** realistic shape per slot (a user active at about 0.1 unit per slot is forecast at about 0.1, not "a small chance of a large spend"); zeros kept; a session at an unusual hour carries forward in the first rows; no split into activity and intensity; the heatmap is a readable diagnostic.

### 8.3 Sums over a horizon

Each row gives the distribution of one future slot. Means add up, but quantiles do not, and combining the rows as if slots were independent gives a spread that is too narrow (§3.1): 8 rows each saying "50% zero, 50% 0.1" are identical whether the user works in whole sessions (total 0 or 0.8) or in scattered slots (total around 0.4).

A variant applies the same weights to past rolling totals over `H` consecutive slots instead of single slots. Rolling totals over long horizons overlap almost entirely (about 4 distinct weekly totals in 4 weeks), so the week is built from daily totals, treating days as independent. The conditioned, overlap-aware version of this idea is Approach H (§10).

### 8.4 Drawbacks

- **Per-slot distributions only:** sums need rolling totals (overlapping, few independent observations) or an independence assumption (spread too narrow).
- **Night activity after an active day:** recent observations are blended additively, and the long recent memory mixes all hours of the day, so it predicts daytime-like activity at night and too little at the midday peak. Possible fix: keep only the short memory as the unaligned recent part, and move the long memory into the seasonal part (the same hours on the last day or two weigh more).
- **Lags the end of a session:** 20 minutes after the user stopped, the short memory still weighs the active slots heavily and predicts continuation.
- **Few effective samples in the first rows:** when the weight sits on 2 or 3 slots, the top of the distribution is those values; if they are the highest, the p95 is the largest of them and jumps from tick to tick. Measured by `n_eff = (Σ W)² / Σ W²`; a cap on the weight of any single observation may be needed.
- **No extrapolation:** a weighted set of past values never exceeds the largest past value, so usage heavier than ever observed is under-forecast until it enters the history.
- **Capped slots:** weight 0 drops the heaviest moments; imputing them at the pre-cap pace is required but is a guess.
- **Days independent** for the 7-day total: a heavy or empty week is not anticipated from its first days.
- **5–7 interacting hyper-parameters** (`recent_short_halflife`, `recent_long_halflife`, `a`, `seasonal_width`, `bump_week`, `seasonal_halflife`, `recent_vs_prior_halflife`): tuning them on 4 weeks risks overfitting the backtest.
- **Cold start:** needs pseudo-observations from the priors until history exists.

## 9. Approach G — F's weights with spend-level transitions

Adds slot-to-slot correlation to F with a Markov chain on spend levels (Approach A with F's weights instead of fixed buckets, and several levels instead of active/inactive).

- **Levels:** spend per slot cut into 0 / L / M / H, boundaries from quantiles of active-slot spend.
- **Transitions:** every past pair of consecutive slots is one example. `T_t[x → y]` = weighted share of pairs going from level `x` to level `y`, with F's weights for slot `t` (time-of-day bell, weekday bump, recent days weighing more), blended with all-hours transitions when examples are few: `(weighted counts + k × T_all) / (n + k)`.
- **Forecast:** start from the current level and propagate slot by slot (dynamic programming over level × cumulative demand, §13). Gives `activity_probability(t)`, the distribution of any sum, and P(saturation).

**Strengths:** sessions and ramp-ups are captured; correlation within a window is built in; the short recent memory becomes unnecessary (the current level plays its role); nights stay near zero because "0 → active" is rare there.

### 9.1 Drawbacks

- **Discretization:** variation within a level is lost, boundaries are arbitrary, and a level jump (L → M) is a coarse proxy for a change of pace. Disliked by the user.
- **One slot of memory:** session lengths are memoryless; how long the user has been working is unknown to the chain.
- **Current level is noisy:** it rests on one 30-minute slot, which a single message can change.
- **Sparse transitions:** each transition matrix is estimated from weighted pairs at similar times; rare transitions depend on the all-hours blend.
- **Blocked slots:** pairs involving a blocked slot are skipped, so heavy sessions are under-represented.
- **Day-level correlation only through the weights:** beyond a few hours, the chain forgets the current state, and away or heavy days are anticipated only through the recent-days weight.
- **No extrapolation** beyond the highest level observed.
- **Complexity:** one transition matrix per future slot and a DP over level × cumulative demand; harder to explain than a profile.

## 10. Approach H — Weighted past totals conditioned on the current pace

Forecasts the quantity the window guard needs, the cumulative organic demand until the end of the current 5-hour window, directly from past totals over the same duration. The past totals already contain the sessions, so the independence problem does not arise, and no discretization is needed.

### 10.1 Method

```text
H        = time left in the current 5h window (the next 5h if no window is open)
pace(x)  = the user's spend over the hour before x (or exponentially weighted, 2h half-life)

for every past start s (every 30 min, window [s, s+H] entirely in the past):
    Y(s)      = organic demand over [s, s+H]          # blocked slots imputed at the pre-cap pace
    weight(s) = bell(time of day of s vs now)          # time-of-day similarity
              × weekday_bump                           # same weekday
              × 0.5^(age / regime_halflife)            # last days count more
              × 0.5^(age / weeks_halflife)             # old weeks fade
              × similarity(pace(s), pace(now))         # continuous, e.g. Gaussian on log(1 + pace)

demand_5h quantiles = weighted quantiles of Y(s)
```

| Requirement | How it is met |
|---|---|
| No usage at night | At 02:00 the bell selects past nights, where `Y` ≈ 0 |
| An active session keeps going | The pace similarity selects past moments at the same pace, whose totals include how sessions really continued or stopped |
| Correlation between slots | Contained in the observed totals |
| Current regime (busy, away) | Recent days weigh more |
| Capped slots | Imputed at the pre-cap pace, not dropped |

### 10.2 Overlapping observations

Overlap does not bias the weighted distribution; it reduces the number of truly independent observations. It is measured per day: sum the weights of each past day, then `n_eff = (Σ_day w)² / Σ_day w²`, so the overlapping windows of the same afternoon count as about one observation.

With about 28 days of history, the time-of-day, weekday and pace conditions leave roughly 10–20 effective observations, thin for a p95. Safeguards:

- Below a minimum `n_eff` (e.g. 15), widen the pace similarity first, then the time-of-day bell.
- Optionally fit a simple shape to the weighted `Y` (P(zero) plus a lognormal for positive values) to read the tail more steadily than the raw quantile.

**Strengths:** targets `demand_5h` directly; realistic spread without modelling correlation; no discretization; nights at zero; explainable ("past afternoons like this one").

### 10.3 Drawbacks

- **`demand_5h` only:** no `activity_probability(t)` and no `demand_7d` (about 4 distinct weekly totals), so a second approach must run alongside for the planner.
- **Thin data for a p95:** 10–20 effective observations; widening the similarities to reach the minimum `n_eff` adds bias (past afternoons that are less comparable).
- **Pace summarizes the state in one number:** it does not know how long the session has lasted or how much the user worked today, and the pace over the last hour lags the end of a session (20 minutes after stopping, the pace still looks active).
- **Variable horizon:** `H` changes at every tick (from 5h down to 30 min), so each tick reads a different set of past totals; forecasts may jump between consecutive ticks.
- **No extrapolation:** the forecast never exceeds the largest past total.
- **Imputation of capped slots is a guess:** a window capped early leaves little pre-cap pace to extrapolate from.
- **Weights concentrate:** the product of five similarities quickly puts most of the weight on a few days, which lowers `n_eff`.
- **No window open:** the next 5 hours is an approximation; the real window starts at the first message and may end later.

## 11. Other ideas considered

| Idea | Why not retained |
|---|---|
| Cumulative rows `[0, +h]` of F's matrix, from past rolling totals | Rolling totals over long horizons overlap almost entirely, so they are far from independent; kept only in the conditioned, overlap-aware form of §10 |
| Predict the series and, separately, its differences, then cumulate the differences | Independent differences make spend a random walk: an active user stays active forever and the variance grows without bound. A pull back to the seasonal level turns it into ARIMA, with its Gaussian shape; abrupt session ends (jumps to 0) are not representable |
| Simulation instead of direct distributions | Simulating does not create correlation by itself; past totals already carry it, so weighted quantiles suffice |

## 12. Building block — Engaged/away days with Bayesian updating

A day-level model of presence, usable to give per-slot approaches (D, E, F) their day-level correlation.

- `E(t)`: probability that the day containing slot `t` is engaged; `E_prior` its long-run value (e.g. 0.9).
- **Update on each completed past day**, oldest to newest: a day with at least one active slot sets `E` close to 1 (an away user sends nothing); a day with no active slot (blocked slots ignored) multiplies the odds `E / (1 − E)` by `q_empty(day)`, the historical share of days of the same kind (weekday, Saturday, Sunday) with no activity. Updating per day rather than per slot avoids treating one quiet afternoon as a dozen independent pieces of evidence.
- **Projection:** `E(t) = E_prior + (E(t0) − E_prior) × 0.5^((t − t0) / h_engaged)`, identical from above or below and at any time of day; nights do not change `E`, they only have a low activity prior.
- **Example:** last active day Sunday, empty Monday and Tuesday with `q_empty(weekday)` ≈ 0.3: odds 99 → 99 × 0.3 × 0.3 ≈ 9, so `E` ≈ 0.9; after a week without activity, `E` ≈ 0.2.
- **Use for correlation:** treating each future day as one engaged/away draw, rather than letting each slot vary independently, gives the "some full days, some empty days" spread needed for `demand_7d`.
- **Drawbacks:** two states only (a light day and a heavy day are both "engaged"); `q_empty` rests on few empty days; one more half-life to set.

## 13. Exact computation by dynamic programming

Used by the per-slot approaches (A, D, E, F, G).

- Means add up across slots.
- Quantiles of sums (`demand_5h`, `demand_7d`) cannot be obtained by adding per-slot quantiles. They are computed exactly by dynamic programming: add slots one at a time on a grid of cumulative demand (about 100 bins), conditioning on the state carried from slot to slot (active/inactive in A, spend level in G, engaged/away day if §12 is used); without a carried state the slots are independent (§3.1).
- P(the current window saturates) = P(`demand_5h` > quota left in the window).

## 14. Comparison

✓ met, ~ partly or with a fix, ✗ not met.

| Requirement | A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|---|
| R1 `demand_5h` p95 | ✓ | ~ | ✗ | ~ | ~ | ~ | ✓ | ✓ |
| R1 `demand_7d` | ✓ | ✓ | ✗ | ~ | ~ | ~ | ✓ | ✗ |
| R1 `activity_probability(t)` | ✓ | ✗ | ~ | ✓ | ~ | ✓ | ✓ | ✗ |
| R2 nights at zero | ✓ | ✗ | ✓ | ✓ | ✓ | ~ | ✓ | ✓ |
| R3 session continuity | ✓ | ~ | ✗ | ✗ | ✗ | ~ | ✓ | ✓ |
| R4 correlation within a window | ✓ | ~ | ✗ | ✗ | ✗ | ✗ | ✓ | ✓ |
| R5 day-level correlation, regimes | ✗ | ~ | ~ | ~ | ~ | ~ | ~ | ~ |
| R6 demand, not spend | ~ | ✗ | ✗ | ~ | ~ | ~ | ~ | ~ |
| R7 little data, few parameters | ✗ | ✗ | ✓ | ✓ | ~ | ✓ | ~ | ~ |
| R8 extrapolation beyond observed | ~ | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| R9 explainable, cheap | ~ | ~ | ✓ | ✓ | ✓ | ✓ | ~ | ✓ |

No approach meets every requirement; the two horizons may be served by different approaches (§1.1).

## 15. Evaluation

- Every forecast is archived and scored against what actually happened.
- Backtest: re-run the predictor at every past half-hour tick, using only data available before that tick. Metrics: calibration of `activity_probability` (do 80% predictions come true 80% of the time), coverage of p90/p95 for `demand_5h` and `demand_7d` (overall and on active ticks), pinball loss.
- Consecutive ticks share most of their future, so coverage figures rest on far fewer independent cases than ticks (roughly one per day for `demand_5h`, one per week for `demand_7d`).
- References to beat: prior only, recent average only, and the harmonic-regression benchmark of §4.

## 16. Parameters

| Parameter | Meaning | Starting value |
|---|---|---|
| `recent_short_halflife` | Memory of the current session (F) | 2 hours |
| `recent_long_halflife` | Memory of the current regime: busy, light, away (F) | 36 hours |
| `a` | Share of the short memory in `recent(l)` (F) | 0.5 |
| `seasonal_width` | Width (standard deviation) of the time-of-day bell (F, G, H) | 1 hour |
| `bump_week` | Extra weight for the same weekday (F, G, H) | 3 |
| `seasonal_halflife` | How fast old weeks lose weight in the seasonal part (F, G, H) | 2 weeks |
| `recent_vs_prior_halflife` | How fast the forecast moves from recent to seasonal weights as the horizon grows (F); weight of recent data and decay of the recent factors (D) | TBD by backtest (D: 3 days) |
| `pace_similarity_width` | Width of the similarity between past and current pace (H) | TBD by backtest |
| Minimum `n_eff` | Effective observations required before widening the similarities (H) | 15 |
| `k` (G) | Pseudo-examples blending sparse transitions with all-hours transitions | 4 |
| `k_season`, `k` (D) | Shrinkage of `week_bucket` towards `day_bucket`, and stabilisation of the recent factors | 4, 4 |
| `E_prior`, `h_engaged` | Long-run share of engaged days, and its relaxation half-life (§12) | 0.9, 3 days |

## 17. Open points

- **Behaviour depends on the quota:** the user may ration usage when quota is low (end of week, after a heavy day). Observed demand is then not independent of the quota state, and extra work that lowers the quota may itself reduce organic usage.
- **Feedback from extra work:** once the system runs, its extra work will sometimes cap the user, so blocked slots become more frequent in the history and their handling (exclusion, imputation) weighs more on every approach.
- **Current slot:** whether the partial spend of `t0` is used as an observation (it is the freshest information on the session).
- **Blocked-slot detection:** the resolution of the quota history may be coarser than a 30-minute slot.
- Whether spend per active slot is really seasonal or not: to be checked on history (spend per active slot by hour and by weekday).
- Cold start: priors of no activity between 00:00 and 10:00 local time and about 2 units per day, until data dominates.
- Codex usage from other clients is invisible locally; it can be added from the part of the meter increase not explained by local tokens.
- The heaviest observed days (≈ 2.1–2.2 units) may have been capped by the quota, so blocked-slot handling matters for the priors too.
