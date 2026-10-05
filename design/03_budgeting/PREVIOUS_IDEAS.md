# Budgeting — previous ideas, and the policies considered

> **Status: the chosen policy is Policy D (§4)**, which is what `DESIGN.md` next to this file implements. The rest of this document is why the alternatives were rejected, and it is still worth reading: §4.5 is the argument that fixes the Monday-daytime concentration with lead time rather than lower ceilings, and §7 is how a backtest would compare these policies against each other.

Candidate policies deciding **when** extra work runs and **how much** of it, for `../DESIGN_v1.md` §2. The policies differ in how much they need to know about the user's future usage, from nothing at all (§2) to a full forecast (§6). Choosing between them decides whether an `organic_usage_predictor` (`../02_prediction/PREVIOUS_IDEAS.md`) is needed at all, and which of its outputs are needed.

## 1. What a policy is judged on

| # | Criterion | Reference |
|---|---|---|
| C1 | Waste: quota left at the 7-day reset | `../CONSIDERATIONS.md` §1, §6 |
| C2 | Blocking of the 5-hour window while the user works (the expensive kind of blocking) | §4.1 |
| C3 | Blocking of the 7-day period, weighted by the time left before the reset | §4.2 |
| C4 | Robustness: behaviour on an away week, a heavy week, an unexpected deadline | §7.4 |
| C5 | Inputs needed: quota readings only, or also human/extra separation and a forecast | §7.8 |
| C6 | Portability to Codex, whose reset time moves and can be pulled forward | §5, §4.4 |
| C7 | Explainability and amount of machinery | §17.1 |

## 2. Policy A — Fixed nightly top-up (no prediction)

The original idea: never forecast anything, and simply make sure the week is consumed at a steady pace, compensating at night for whatever the user did during the day.

```text
every night, between 00:00 and 10:00 local time:
    k              = number of full days left before the 7-day reset
    reserve_target = F + D × k              # D ≈ 2 units per day, F ≈ 1.5 units for the last day
    extra_tonight  = max(0, R − reserve_target)      # R = remaining 7-day quota, in units
    run extra work until the quota drops to reserve_target, or until 10:00
```

`D` is the amount left free for each remaining day of the user's work, `F` the amount left for the final day (for Claude, the Monday between 10:00 and the 21:00 reset). Both are constants, read once from history (the observed daily p90 is about 2 units; the user estimated about 1.5 units for a Monday).

### 2.1 Strengths

- **No predictor at all.** Only the quota readings are needed: no session parsing, no tagging of human versus extra work, no profile (C5). This also sidesteps §7.8 entirely (past usage mixes human and extra).
- **Self-correcting every 24h.** A heavy day lowers `R`, so the next night runs less; a light day leaves more. No model is needed for this feedback.
- **Runs when the user is away.** Nights are where interference is near zero (§4.3), so C2 is mostly satisfied by construction.
- **Trivial to explain, to simulate and to debug** (C7).
- The behaviour the predictive planner (§6) is supposed to produce is exactly this one, so the rule is the benchmark any fancier policy must beat.

### 2.2 Drawbacks

- **Night hours are hard-coded.** They come from the user's current rhythm and would be wrong for another rhythm; the user explicitly asked for a system that does not hard-code such rules.
- **`D` and `F` are an implicit forecast.** They compress the whole usage profile into two constants, chosen from the same history a predictor would use, but they never adapt to the current week.
- **Away weeks are wasted.** On vacation the rule still reserves `D` per day, so about 2 units per remaining day are left unused (C4). Detecting "the user has not worked for 2 days" is the single most valuable prediction, and this policy cannot use it.
- **The last hours before the reset are not used.** If the user does not work on the final day, `F` (about 17% of the Claude week) is wasted. Using it requires knowing, on that day, that the user is not coming.
- **No protection inside the 5-hour window.** If the user starts a session at 02:00, the rule keeps spending; a saturated window then blocks them for up to 5 hours (C2). A live guard is needed, at minimum "the user has sent a message in the last N minutes".
- **Reset times that do not fall near a night.** For Codex the reset moves and can be pulled forward by the server (§5), so "the night before the reset" may not exist and `k` may be wrong (C6).
- **Days and 5-hour windows are not aligned.** Topping up "to `reserve_target`" says nothing about how many windows remain to absorb that quota, so part of the reserve can already be unreachable (§6.3).
- **A single burst.** All the extra work of a night is concentrated in a few windows, which needs a high burn rate and leaves no slack to correct errors (§4.5).

### 2.3 Variants that remove some drawbacks without a forecast

| Variant | Effect |
|---|---|
| `D` from history rather than fixed (daily p90 per weekday) | Keeps the weekly rhythm without a model |
| "No human activity for X hours" lowers `D` | Cheap away-day detection from quota readings only |
| Live guard: no extra work if the user was active in the last N minutes, and keep p% of the 5-hour window free | Covers C2 without any prediction |
| Windows instead of days: top up to `reserve_target` expressed in windows still to come | Aligns with §6.3 |

## 3. Endgame rules for the last hours before the reset

Policy A wastes the reserve it keeps for the final day whenever the user does not work that day (§2.2). These rules recover it without any forecast: they simply check, late enough, whether the user actually showed up. Policy D (§4) generalises them: the same behaviour comes out of the back-fill, without hard-coded hours.

Claude, reset Monday 21:00 Paris. Each 5-hour window is identified by when it opens.

| Step | Rule |
|---|---|
| 1 | Make sure a 5-hour window has opened before **11:00** (the reset minus two windows), so that two full windows still fit before 21:00. Opening it is the job of the window starter (`../04_start_windows/CONSIDERATIONS.md`). |
| 2 | **One hour before that window ends:** if the user did no human work during it and at least 1.5 units are left, spend 0.6 unit immediately, keeping 0.9 for the last window. |
| 3 | Make sure a 5-hour window has opened before **16:00** (the reset minus one window), so the last window ends exactly at the reset. |
| 4 | **One hour before that window ends (around 20:00):** if the user did little or no human work during it and quota is left, spend all of it before the window closes. |

Properties:

- **No prediction.** Each step looks at what already happened in the window that is ending, not at what might happen.
- **The user keeps priority.** If they worked during the window, the rule does nothing and their remaining quota stays theirs.
- **Waiting until one hour before the end** is what makes the check meaningful: a user who has not appeared in four hours is unlikely to appear in the last one, and if they do, at most 0.6 unit of the window has been taken.
- **The 0.1-unit style margins** (0.6 then 0.9 rather than 0.9 then 1.0) absorb the ±30% variation of a window's real capacity (`../CONSIDERATIONS.md` §5) and leave the user some room.
- **Codex** needs the same rules anchored on its own reset time, which is known only once the period has started and can be pulled forward by the server.

## 4. Policy D — Back-filled window plan (no prediction)

Proposed by the user. Instead of asking "how much should be spent per day", it rebuilds the whole chain of windows from now to the 7-day reset, gives each one a ceiling, then fills them **from the last one backwards**. Everything the user could still want is therefore left untouched for as long as possible, and the system only acts when nothing later can absorb the quota any more.

### 4.1 The three steps

**Situation:** Sunday 20:17, the current 5-hour window ends at 21:35 with 0.5 unit already used in it, 2.7 units left on the 7-day quota, reset Monday 21:00.

**Step 1 — project the chain of windows** from now to the reset, assuming each new window opens a short gap after the previous one expires (5 minutes, opened by the window starter, `../04_start_windows/CONSIDERATIONS.md`):

```text
[ now      – Sun 21:35 ]
[ Sun 21:40 – Mon 02:40 ]
[ Mon 02:45 – Mon 07:45 ]
[ Mon 07:50 – Mon 12:50 ]
[ Mon 12:55 – Mon 17:55 ]
[ Mon 18:00 – Mon 23:00 ]   the 7-day reset falls inside this one
```

**Step 2 — give each window a ceiling.** A window is filled to at most 0.9 unit, never 1.0, so the user keeps room and the ±30% variation of a window's real capacity is absorbed (`../CONSIDERATIONS.md` §5). The window straddling the reset is split into two chunks, and its ceiling is shared in proportion to time, so that what is spent before the reset does not deprive the new week:

| Chunk | Ceiling |
|---|---|
| now – Sun 21:35 | 0.4 (0.9 minus the 0.5 already used) |
| Sun 21:40 – Mon 02:40 | 0.9 |
| Mon 02:45 – Mon 07:45 | 0.9 |
| Mon 07:50 – Mon 12:50 | 0.9 |
| Mon 12:55 – Mon 17:55 | 0.9 |
| Mon 18:00 – Mon 21:00 (before the reset) | 0.6 |
| Mon 21:00 – Mon 23:00 (after the reset) | 0.3, charged to the new week and therefore out of this plan |

**Step 3 — back-fill from the last chunk before the reset:**

| Chunk | Planned | 7-day quota left after it |
|---|---|---|
| now – Sun 21:35 | 0 | |
| Sun 21:40 – Mon 02:40 | 0 | |
| Mon 02:45 – Mon 07:45 | 0.3 | 2.4 |
| Mon 07:50 – Mon 12:50 | 0.9 | 1.5 |
| Mon 12:55 – Mon 17:55 | 0.9 | 0.6 |
| Mon 18:00 – Mon 21:00 | 0.6 | 0 |

The current chunk is planned at 0, so the run stops immediately. This is the normal outcome most of the time, and it costs one arithmetic pass.

### 4.2 When the plan is executed

The plan is recomputed at every tick, but only the current chunk can be acted on, and it is acted on **as late as possible inside that chunk**, so the user keeps first claim on it.

| Scenario (same situation, user still idle) | Plan for the current chunk | Action |
|---|---|---|
| Monday 03:00, window opened at 02:45 | 0.3 unit before 07:45 | Nothing yet: there is still time |
| Monday 06:45, same window | 0.3 unit before 07:45 | Start now: 0.3 unit has to be spent in the remaining hour, which is about 45 minutes of work |

The trigger is therefore not a fixed "one hour before the end" but the moment when the time left in the chunk is only just enough to spend what is planned: `time left ≤ planned amount / burn rate × safety factor`. A fixed one-hour rule is the special case when 0.9 unit takes about an hour to burn.

### 4.3 Strengths

- **No prediction of any kind.** Only quota readings, window timings and the achievable burn rate are needed; no session parsing, no human/extra separation, no profile (C5).
- **The user always has priority.** Back-filling means nothing is ever spent while a later window could still absorb it, so the system takes only what would otherwise expire (C1 with minimal C2).
- **The reset boundary is handled explicitly.** Splitting the straddling window prevents a saturated window from carrying into the new week (`../CONSIDERATIONS.md` §13).
- **Self-correcting.** The user's own usage lowers the remaining quota, so the next tick simply plans less; a missed window or an early Codex reset changes the chain and the plan follows.
- **Cheap and explainable.** The whole plan is a table a human can read, and the common case aborts in milliseconds.
- **Portable to Codex**, which only needs its own reset time and ceilings.

### 4.4 Drawbacks and open points

- **It concentrates work in the last windows, which for Claude are Monday daytime.** The plan above asks for 0.9 unit in each of the Monday 07:50 and 12:55 windows, i.e. during the user's working day. Deferring to the end of each chunk limits the exposure, but the risk of the expensive kind of blocking (§4.1) remains. This is the main weakness of the policy; §4.5 shows that the fix is the lead time (burn as late as possible, fast where the user may be around), not a lower ceiling.
- **The burn rate matters for the trigger, not for the ceilings.** Spending 0.9 unit over a full 5-hour window is always achievable; the user estimates about 1 unit per hour, so 0.9 unit needs roughly an hour and the late trigger has to fire accordingly. The rate is still unmeasured (`../CONSIDERATIONS.md` §6.6), so a chunk ceiling capped at `burn rate × time left in the chunk` remains a cheap safety.
- **Extra work is lumpy**, so a chunk's budget cannot be hit exactly (§12.4). Out of scope here: this stage produces a budget and a trigger, and landing on it is the task scheduler's problem.
- **It assumes the projected windows will exist.** If the window starter fails (machine asleep, §16), the chain is shorter than planned and the quota was counted on windows that never opened. The plan is a projection recomputed every tick, not a commitment.
- **It assumes the user spends nothing in the future.** That is what makes it safe early (everything is left to them) but it also means the final windows are planned as if the user will not work on Monday. If they do, their usage reduces the remaining quota and the plan shrinks; the conflict is confined to the last window or two, where 7-day blocking is the mild kind (§4.2).
- **A margin of 0.1 unit is a guess.** It must cover the user's chance of appearing mid-window plus meter resolution; expressing it on the meter percentage rather than in dollars avoids the ±30% capacity variation.

### 4.5 Lead time, not ceilings: how to avoid harming the user

**The ideal case.** With an infinite burn rate and perfect precision, the system would trigger one minute before each chunk ends and burn exactly what the back-fill says. The user's activity would then not matter at all: whatever is spent at that moment is quota that the remaining chunks provably cannot absorb, and the user cannot be blocked for more than that last minute. Pure back-filling plus a last-moment trigger has **no cost for the user**, by construction.

**What breaks it is the finite burn rate.** Spending 0.9 unit takes about an hour, so the trigger must fire about an hour before the chunk ends, and the lead time is the only reason the system ever competes with the user:

```text
lead_time(chunk) = planned amount / burn rate × safety
```

**The knob is therefore the burn rate, not the ceiling.** The system should buy back lead time where the user is likely to be around:

| Chunk | Strategy |
|---|---|
| The user is likely active (Monday daytime) | Highest possible burn rate — parallel agents, the most expensive models at high effort, token-heavy tasks — so the trigger fires as late as possible. Blocking, if any, then lasts only until the window ends. |
| The user is likely away (nights) | Low burn rate is fine: trigger early in the chunk, run long cheap tasks. Nothing is at stake. |

This couples the budget calculator to task selection: the task scheduler needs a mix of tasks with known burn profiles, short and expensive for the late bursts, long and cheap for the nights. Note that parallel agents raise the burn rate without raising the ceiling, which is exactly the use for them (`../CONSIDERATIONS.md` §14.1); account-level throttling is an open question (§14.4).

**Why a time-of-day ceiling is the wrong fix.** Lowering the ceiling during working hours (`ceiling = 0.9 × availability(chunk)`) pushes work into the nights before, which means spending quota **earlier than necessary**. Same situation as §4.1 with 2.7 units left: the ceilings would spend 1.9 units on the Sunday and Monday nights, so the Monday 07:50 window starts with 1.6 units instead of 2.7. A user who then works normally saturates the weekly quota around 14:30 and waits until the 21:00 reset — 6.5 hours of blocking that pure back-filling would never have caused. The trade is a bad one: it converts a bounded risk (a few minutes of 5-hour blocking at the end of a chunk) into an unbounded one (hours of 7-day blocking).

Availability-weighted ceilings are therefore kept only as a fallback, for the case where the burn rate is too low to spend the surplus in the late chunks at all, or where the user's absence is already certain.

**While the burn rate is unknown, bias towards waste.** Triggering too early risks blocking the user; triggering too late risks leaving quota unspent. Until the achievable rate is measured from the system's own runs, the second error is the cheaper one.

### 4.6 Relation to the other policies

Policy D is the waste-free floor of Policy B (§5) made concrete: same "spend only what would otherwise expire" principle, but expressed per window, with explicit ceilings, a handled reset boundary and a late trigger. It makes Policy A's constants (2 units per day, 1.5 for the final day) unnecessary, and it turns the endgame rules of §3 into a special case of the back-fill rather than separate hard-coded hours. What remains outside it is the live guard for the user's presence, and a low-precision idea of when the user is likely active — used to decide where to spend lead time and burn rate (§4.5), not to lower any ceiling.

## 5. Policy B — Waste-free floor only (no prediction)

Run extra work only when the remaining quota exceeds what the windows still to come can absorb (`../CONSIDERATIONS.md` §1–2, §6.2), i.e. when waste becomes certain.

- **Strengths:** no prediction, no parameters, provably no unnecessary spending; the quota is never spent while it could still be used by the user.
- **Drawbacks:** acts far too late. With 5.5 units left on Saturday 10:00 (Claude), the floor binds only on Sunday 17:30, and from then every window must be saturated until the Monday 21:00 reset, blocking the user for most of Monday (C2, C3). It needs the maximum burn rate to be high enough, and any missed window becomes waste (C1). This is the argument of §4.5 and `../02_prediction/PREVIOUS_IDEAS.md` §1.1.

## 6. Policy C — Predictive planner (`../DESIGN_v1.md` §2)

Re-plan every 30 minutes over the rest of the 7-day period: keep a reserve equal to a high quantile of the forecast human demand until the reset, and place the rest in the slots where the user is least likely to be active, subject to the 5-hour caps and a window guard.

- **Strengths:** nothing is hard-coded (night, `D`, `F` and the final-day reserve all emerge from the data); away weeks and heavy weeks are handled; the last hours before the reset are used when the user is idle; it adapts to Codex's moving reset (C4, C6).
- **Drawbacks:** needs the predictor and therefore the separation of human from extra usage (C5); several parameters; its behaviour is harder to explain than a rule (C7); its advantage over Policy A is unproven, and with about 4 weeks of history it may not be measurable.

## 7. How to choose

Simulate the policies (and the variants of §2.3) on the recorded history, replaying the user's human usage as demand and the quota rules of `CONCLUSIONS.md`:

| Measure | Target |
|---|---|
| Units wasted per week (C1) | As low as possible |
| Minutes where the user hits a saturated 5-hour window caused by extra work (C2) | Near zero |
| Units of extra work actually placed | As high as possible for the same blocking |

Policy D is the baseline, since it needs no prediction and follows the quota mechanics directly. Policy C is worth its machinery only if it wastes clearly less for the same blocking, or blocks clearly less for the same waste. The most likely useful addition to D is not a full forecast but the single question "is the user around?", used as a live guard and to release the last windows when they are clearly not coming.

## 8. Open points

- The maximum burn rate per agent is unknown, so how much of a night can actually be spent is unknown (§6.6).
- Simulating a policy on history requires replaying human demand, which is itself censored by past caps (§7 of the prediction designs).
- Codex's early server resets make any day-counting rule fragile (§5).
