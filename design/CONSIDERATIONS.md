# Agent Quota Maximizer — Considerations

Considerations, limitations, problem structure and constraints to keep in mind while designing this system. This file contains no design choices: every choice in `DESIGN.md` should be justifiable from what is written here, and anything still unknown is tagged **Unknown**. Each bullet is one independent consideration: something to think about, and why it could matter.

**The problem.** Claude Code and Codex quotas are use-it-or-lose-it. Each agent enforces a 5-hour window nested inside a 7-day period, and whatever is left when the 7-day period resets is gone (no rollover). A heavy but irregular user rarely burns the whole allowance organically, so a slice of a paid resource is regularly wasted. Yet the same resource is precious while it lasts: interactive work must never be blocked by automation. The opportunity is to spend the surplus on useful, low-risk background work (software-maintenance reviews across the repos in `~/dev`), which raises four questions: when to spend, how much, on what, and how to run it.

**How to read this file.** Sections 1–5 build the problem from a worked example, the user's tolerance and the measured quota facts. Sections 6–18 complete the picture by theme: structure of the waste, the user's human usage, triggering windows, tasks (value, risk, cost, selection), execution, agent differences, safety and outputs, environment.

## 1. Illustrative example:

**For the following, let's consider an example:**

- Current time: 8h00 (for illustration)
- `CAPACITY_5H = `Max capacity (in dollars) of every 5-hour window = $`70`
- `max_spend_5h` = Remaining capacity for the current 5-hour window = $ 50
- `time_to_reset_5h` = When the current 5-hour window will expire = 1 hour
- `CAPACITY_7D = `Max capacity (in dollars) of every 7-day period = $`500`
- `max_spend_7d` = Remaining capacity for the current 7-day period = $ 180
- `time_to_reset_7d =` When the current 7-day period will expire = 11 hours

In this example, `wastefree_spend_5h` = $180 - floor(11/5) * $70 = $40

So if during the next 1h, the total work (human work + extra work) consumes...

- ...less than $40: then we are sure some quotas will be wasted at the end of the 7-day period
- ...exactly $40: Perfection (ideal, only theoretical)
- ...between $40 and $50: then we will not saturate the current 5-hour window, but we might end up saturating the current 7-day period (even if unlikely)
- ...more than $50: then we will saturate the current 5-hour window (hence potentially blocking human work from the user) and we might end up saturating the current 7-day period (even if unlikely)

This is a good example to show that the system might need to act way before the end of the 7-day period to fully use all available quota.

## 2. Visual representation of the problem:

```plaintext
Shape of WFF(t) : The Waste_free_floor consumption curve during the 7-day window. The total usage (human + extra work) need to stay above this curve to ensure no waste at the end of the 7-day period. The shape of the curve is flat until it steps by `capacity_5h` every 5 hours until it reaches the `capacity_7d` at the end of the period exactly.

                                                           ______|
                                                      _____|     |
                                                _____|           |
                                          _____|                 |
                                    _____|                       |
____________________________________|                            |
0                                                                7d

Now, if the real consumption curve (human + extra) goes below this floor, we lose some quota. The goal of this system is to ensure that the consumption curve stays above this floor.
```

## 3. Adding predicted human spend:

Same example, but we predict human user spend as well:

`predicted_organic_spend_5h`  = $20 +/- $10 (95% Confidence interval is [$10, $30])

**What changes.** The target for the *total* stays [$40, $50]. The prediction only says how that total splits between human and extra work, and extra work is what remains:

- `wastefree_extra_5h` = $40 - $20 = **$20** (the least extra work worth doing)
- `max_extra_5h` = $50 - $20 = **$30** (the most extra work allowed)

So the answer to "how much extra work in the next hour?" moves from "up to $50" to a budget of about **$20 to $30**, shifted down by the human forecast and clamped at zero (if human alone reaches $40 no extra work is needed, and at $50 none is possible). The width of the band ($10) does not change: it comes from the 5-hour window and the 7-day remainder, not from the forecast.

**What the uncertainty does.** The forecast adds a second, independent source of error on top of the band. Taking $25 of extra work (the middle of the band):

| Actual human spend           | Total with $25 of extra work | Outcome                                                                      |
| ------------------------------ | ---------------------------- | ---------------------------------------------------------------------------- |
| $10 (low end of the interval)  | $35                          | $5 of 7-day quota is certain to be wasted                                    |
| $15 to $25                     | $40 to $50                   | Inside the band                                                              |
| $30 (high end of the interval) | $55                          | The 5-hour window saturates before the hour ends and human work is blocked |

The interval on human spend is $20 wide, twice the band ($10), so **no single amount of extra work stays inside the band across the whole interval**. Guaranteeing no waste (human as low as $10) needs at least $30 of extra work, while guaranteeing no blocking (human as high as $30) needs at most $20. The two guarantees are incompatible, so one of the two errors has to be risked, and §4 says which one hurts the user more. Even in the best case a $10 band cannot be hit reliably: if the forecast were normal with a 95% interval of ±$10 (σ ≈ $5), the total lands inside [$40, $50] only about two times in three.

The forecast also improves as the hour goes by: human spend actually observed so far replaces part of the prediction, so the remaining uncertainty shrinks (see §7).

## 4. Not all blocking is equal

- **There are two kinds of blocking.** A saturated 5-hour window is unpredictable and interrupts work in progress: it is extremely annoying for the user to have to pause and wait. A saturated 7-day period is visible in advance, so the user can plan around it (walk outside, go to the beach or the gym etc...).
- **Blocking on the 7-day period hurts in proportion to the time left before the reset.** Being blocked with a few hours left is mild, being blocked with several days left is severe. Over-reaching the 7-day quota is a small harm only close to the reset.
- **The user's presence changes the cost of interference.** Extra work at night, when the user is absent, interferes with nothing, while the same work on a weekday afternoon can block real work. The observed rhythm is in §7, and it does not support a "quiet Sunday": Sunday averages $27 of human spend against $6 on Saturday.
- **The deadline falls at a different moment of the user's life for each agent.** Claude's week always ends on Monday 19:00 UTC (21:00 in Paris in summer), during the user's working day, and Monday daytime before it is likely regular human work (the user estimates roughly 1.5 five-hour windows of quota). Codex's deadline depends on the first message of the week and could fall at 8 AM, in which case the night before is free of human use.
- **Squeezing extra work into a few hours has a cost.** It needs a high burn rate, saturates several 5-hour windows in a row (§6) and leaves no time to correct errors (§12), and every window has to be opened right at the previous expiry (§5, §8). Spreading it over more hours lowers the intensity and leaves slack, but overlaps more with the user's presence.
- **The finite burn rate is the only reason extra work has to compete with the user.** If quota could be burned instantly, extra work could always wait until the last minute of a window and take only what no later window could absorb. Every minute of lead time needed to spend an amount is a minute during which the user can collide with it.
- **Spending earlier than strictly necessary trades a bounded risk for an unbounded one.** Work placed in an earlier window exposes the user to running out of the *weekly* quota later, which can block them for hours, while work placed at the very end of a window can block them only until that window expires.
- **An active user is consuming the contested quota anyway.** Stepping aside when the user is working costs little: what is not taken by extra work is taken by them, and the surplus to rescue shrinks on its own.
- **The reset does not give the user a fresh 5-hour window.** Whatever fills a 5-hour window around the reset, extra work or the user's own Monday afternoon usage, carries into the new week (§13).

## 5. A separate project is converting quota percentage, token usage and USD values.

This system relies on data about how much quota is currently used vs remaining, etc...  
The quota data (for both Claude and Codex) is regularly fetched and store in `~/opt/agent-usage-tracker/data/` for any other system like this one.

1. These data contain values in % quotas.`five_hour%` and `seven_day%` are each normalized to their own window's 100%, so how much `seven_day%` one fully-saturated `five_hour` window actually burns is an unpublished conversion factor. Translating those percentage into USD resolves the issue.
2. Also, we might want to estimate the size of extra work (extra tasks) of this project in terms of #tokens. We also need a way to translate those into USD.
3. Finally, this system need to completely understand how windows time bound (start/end) are defined for different windows (5 hours and 7 days) and different agents (Claude vs Codex)

**Canonical references: `~/dev/agent-usage-tracker/USAGE_DATA_SOURCES.md`** — what usage data exists upstream, in which unit (percent / tokens / USD), at which granularity, for both agents, and what is not available anywhere — **and `~/dev/agent-usage-tracker/USAGE_DATA_REFERENCE.md`** — what `agent-usage-tracker` captures, how, where, and every trap in the recorded data. They are owned by `agent-usage-tracker` because that project writes the feeds; every project that reads or converts them links to them rather than restating them. The dollar conversions they assume are derived in that repo's `adhoc_quotas_analysis/CONCLUSIONS.md`, which remains the place to add new analysis.

At the time of writing, the conclusions are:

|                                     | Claude (Pro)                                                           | Codex (Plus)                                                                |
| ----------------------------------- | ---------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| 100% of the 5-hour window           | $32 (middle 80% of windows: $20–39)                                    | $20.5 (range $16.7–26.3)                                                    |
| 100% of the week, in 5-hour windows | 8.85×, so about $283–314                                               | 6.2×, so about $127                                                         |
| How the 5-hour window opens         | At the first message after the last one expired                        | Same                                                                        |
| How the 7-day window resets         | Fixed: every Monday 19:00 UTC                                          | Starts at the first message after idle; sometimes reset early by the server |
| What the API reports when idle      | `null`, 0%                                                             | A fake countdown: 0%, reset time = now + 5 h (or + 7 d)                     |
| Idle gaps between 5-hour windows    | 26 of 31 pairs (median 11.5 h); windows cover only 24% of elapsed time | 21 of 28 pairs (median 10.0 h)                                              |
| Does a 7-day reset also reset the 5-hour window? | No: the running window keeps its % and its end (seen twice) | No at a natural weekly expiry (seen once, at 1–2%); yes at the 08-27 server-side early reset, which re-anchored both |

Consequences to keep in mind:

- **The 7-day deadline is not equally known.** It is exact for Claude, and idle time at the start of a week is not banked. For Codex it is unknown until the first message of the week and can be pulled forward by early server resets (08-27, 08-31, 09-12), so a Codex weekly meter can suddenly return to 0%.
- **Capacities are measured, not fixed.** The 5-hour budget varies about ±30% across windows, partly because of the token mix (cache reads weigh far less than list price). The end of the +50% weekly promo cut the weekly allowance by about 11%, not the predicted third, for unknown reasons. Plans and prices can change.
- **Staleness.** Idle sessions re-push readings taken hours earlier, so a reading must be keyed by when it was *observed*, never by when it was logged.
- **Unseen usage on Codex.** Other clients on the same ChatGPT account (cloud tasks, IDE extension, ChatGPT app) raise the % with no local tokens. On Claude, every % step since 09-13 matched a local message.
- **Percent is the only currency both agents report, and the only one that is enforceable.** It is what actually runs out. Dollars exist only on Claude — now on three channels (every status-line render, every API request via OpenTelemetry, and a `-p` result object) — and always at *list* basis (`costBasis: "list"`): a different currency from the subscription meter, with a ±30% exchange rate. Tokens convert to percent by an unknown, variable factor. Enforce on percent, compare on dollars, diagnose on tokens.
- **No meter reading carries a session id, and none can.** The meter is account-level, so attributing movement to the user rather than to this system cannot be done from the quota logs alone. It can be done from time intervals, because this system knows when its own work ran.
- **The meter's resolution is 1% for both agents, and it is upstream's, not ours.** Not one fractional value appears among 255,156 recorded Claude push percentages and 11,314 poller percentages **[verified]**, so the quantum 1% ≈ $0.32 of a window and ≈ $2.8 of a week is inherent. The single sub-percent source is Codex's retroactive `plan_limit_history` (basis points, finished windows only).
- **The polling fallback is unreliable.** 8,516 of 12,848 Claude poller rows carry a real error (SSL, HTTP 429, missing token); the free per-render push path does ~92% of the work. Codex has no push path and shares that transport, so the agent with the least visible usage depends most on the feed that fails most.
- **Claude exposes per-session and per-request cost; Codex exposes neither.** Every status-line render receives cumulative session USD and cache statistics alongside the quota percentages, and Claude Code's OpenTelemetry export emits one event per API request with tokens and list-price USD — for headless runs too. Hooks receive none of it. All of it is recorded by `agent-usage-tracker` since 2026-09-30.
- **Headless and interactive Claude runs are distinguishable in the recorded data, by two independent marks** **[verified 2026-10-04]**. A headless `-p` run renders no status line, so it produces no push row at all; and its telemetry events carry `query_source: "sdk"`, where an interactive conversation's carry `repl_main_thread`, `generate_session_title` or `prompt_suggestion`. Two headless runs in the recorded data each produced exactly one telemetry row and zero push rows. This is what makes our own spend separable without any bookkeeping of our own.
- **Telemetry is complete per request but not guaranteed to arrive.** Claude Code pushes OpenTelemetry events in batches and buffers nothing on disk, so a receiver that is down loses them, and sessions started before the keys were set send none. Per-request dollars are therefore a *measurement* to reconcile against, never the authority: the authority stays the meter.
- **A transcript's mtime is not a usage signal** **[verified 2026-10-04]**. Two separate contaminations, measured over all 168 Claude transcripts on this machine:
  1. **mtime moves with no new entry at all.** 24 transcripts have an mtime more than a *day* after their last `user` or `assistant` entry, 45 more than an hour — 27% of the total. Five files, one per project directory, were all stamped within the same 3-second burst while their last conversational entry was 12 to 19 days old. Something sweeps them; `agent-session-manager` runs a reindex every 30 minutes, and whatever the cause, no user was involved.
  2. **The last entry is usually not a message.** 115 of 168 transcripts end in a `system` entry — `stop_hook_summary`, `turn_duration` — or a hook `attachment`. These trail a real turn by seconds, so they are a lagging echo rather than a false positive, but they mean the *last entry* and the *last interaction* are different things.
- **A Codex window cannot be identified by its reset timestamp alone.** `resetsAt` is an epoch integer that drifts by a second between readings of the same window (two such pairs in the recorded data **[verified]**), and 3,136 of 4,666 successful poll rows are the fake idle countdown, whose `resetsAt` moves with every tick. Keyed naively, 35 days of Codex data look like 3,178 windows instead of 50 **[verified]**.

## 6. Structure of the waste

- **The 5-hour window only limits how fast quota can be consumed.** Waste is materialized at the 7-day reset: 5-hour quota left unused is harmless as long as the 7-day remainder can still be absorbed by the windows to come.
- **Waste can only effectively happen near the end of the period.** The floor curve of §2 is zero until the remaining 7-day quota exceeds what the windows still to come can absorb, i.e. roughly the last `CAPACITY_7D / CAPACITY_5H` windows: about 8.85 windows (44 hours) for Claude and 6.2 (31 hours) for Codex (§5). Earlier than that there is nothing to rescue, so the amount of extra work worth doing can legitimately be zero.
- **Some quota can already be unspendable.** Whatever exceeds what the windows still to come can absorb is lost whatever happens, and the last window before the reset is not cut at the reset: a weekly reset leaves the 5-hour window untouched (§5), so that window usually straddles it. Only what is burned before the reset counts for the old week, so how much of it is usable depends on how fast quota can be burned (see below).

```plaintext
reachable_7d = max_spend_5h + (full windows still to come + usable fraction of a trailing partial window) × CAPACITY_5H
waste_unavoidable = max(0, max_spend_7d - reachable_7d)
```

- **Slack or no slack changes the nature of the problem.** With slack (`max_spend_7d ≤ reachable_7d`) timing is free and the tension is between waste and blocking the user. Without slack, part of the waste is already certain and pacing can no longer change it, so the question shifts from how much to spend to what to spend it on.
- **The floor curve of §2 is optimistic.** It assumes back-to-back windows ending exactly at the reset, while idle gaps are the norm and the reset does not realign windows (§5). Fewer windows remain than `time_left / 5h` suggests, so the real floor sits at or above the drawn one, and the shortfall is not visible on the curve.
- **How fast quota can be burned is unknown.** The maximum dollars per minute an agent can consume decides how much of a short trailing window is usable. **Unknown:** the maximum achievable burn rate per agent.

## 7. Predicting the user's human usage

- **What matters is the human usage until the end of the current window.** Whether quota ends up unused (or the window saturated) is only decided at the end of the window, so that is the forecast horizon (the 7-day reset in the last window). How often readings arrive does not change it: knowing the next five minutes of human usage is not the point, the total is.
- **A few weeks of history is short.** Any statistical description of the user's habits would be poorly determined, and it could be far off with so few examples of each weekday and time of day.
- **Organic usage follows a weekly rhythm.** Weekdays differ from weekends, nights are quiet, Monday morning is busy. The same hour can be very different depending on where in the week it falls, so "recent average" may mislead.
- **Organic usage can spike unpredictably.** An urgent interactive session can appear whatever the history says. A margin protects the user from being locked out, and it also means landing exactly at 100% every cycle is not realistic.
- **The margin that is worth keeping depends on whether the user is around.** A margin is valuable on a weekday afternoon and nearly worthless at 3–5 AM, where reserving quota protects nobody. Keeping one where it is not needed leaves quota unused.
- **The margin has no value at the deadline.** At the very end of the period, human quota still unused is itself waste.
- **Observed profile (Claude, since 08-24, list-price USD).** Usage on 19 of 29 days; average spend per weekday: Mon $38, Tue $16, Wed $23, Thu $16, Fri $10, Sat $6, Sun $27; quiet between 23:00 and 06:00 UTC. An average active day is about 65% of one 5-hour window and 7% of the week, and weekly peaks were 81%, 88%, 10%, 27% and 47%, so a large part of the weekly quota routinely goes unused. Only five weeks of data.
- **Observed usage understates demand.** When a 5-hour window saturates, what the user wanted is unknown: only what they were allowed to spend is recorded. In the Claude history, 5 of 35 five-hour windows reached 100% (about 4 hours in total spent at the cap over 30 days), so the heaviest moments are exactly the ones that are cut off.
- **Usage may depend on the quota itself.** A user who sees little quota left may hold back, so past usage reflects both their intentions and the limit they faced.
- **The system changes what it is trying to learn.** Extra work that saturates a window makes the user's own usage impossible during it, adding more censored history over time.
- **How precise a forecast must be depends on what is done with it.** A schedule that only needs to know whether the user is around asks much less than one that needs the amount they will spend.
- **Past usage mixes human and extra work.** Learning the user's habits from total consumption would also learn this system's own activity, and percentages are aggregate: they cannot say who generated the usage. Local Claude transcripts appear complete, but Codex usage from other clients is invisible locally (§5).
- **Absence of data is not absence of demand.** A machine asleep, a window that was never opened, and a window saturated at 100% all look like "no further usage" in the percentages, and all three mean something different from a user who wanted nothing. Any history used for learning has to carry those three states explicitly, or the model will read every night as evidence of zero demand.

## 8. Automatically triggering windows

In some cases (5h-windows, and 7d-windows for Codex), we might want to automatically trigger the start of a new window, to ensure that we can either reach the 7d quotas (in the case of triggering a 5h-window) or we can maximize total quota per year (in the case of 7d-window for Codex). Something to think about.


- **A window exists only because a message opened it.** Nothing starts a window on a schedule, so a period of silence is not a pause in a chain of windows: it is a window that never existed and whose quota was never available.
- **A gap between windows costs a whole window at the end of the chain, not just the gap.** Near a 7-day reset, the number of windows that still fit decides how much of the weekly quota is reachable, so a few hours of silence can remove a full window's worth of capacity (`04_start_windows/CONSIDERATIONS.md` §2).
- **Opening a window costs almost nothing, and an opened window that goes unused costs nothing at all.** The 5-hour cap limits speed only; it does not consume the weekly quota.
- **A window opened close to the reset straddles it.** Part of its capacity belongs to the old period and part to the new one (§5, §13).
- **The message that opens a window is itself usage.** It appears in the history like any other, so it can be mistaken for the user's own activity (§7).
- **For Codex the 7-day period also starts on a message.** Idle time at the start of a week is not banked, so how promptly periods are started decides how many of them a year contains.

## 9. Task value: what makes some work worth more than other work

- **Repos do not have the same importance.** A live deployment (`~/opt/<x>` plus a scheduler) has ongoing operational impact, a financial or trading repo has high stakes whatever its activity, and an archive or personal repo may be worth little. Naming and layout give hints but are imperfect proxies for the user's real priorities. **Unknown:** the final in-scope list and weights.
- **Some kinds of work are more urgent than others.** Finding bugs is the explicit top priority of the user, because a bug in live code keeps costing until it is found.
- **Some kinds of work compound and others do not.** Keeping agent instruction files (`CLAUDE.md`, `AGENTS.md`) accurate, tests and backlog hygiene pay back in every later session, while refactoring opinions or architecture reviews may be worth little without evidence.
- **Some findings are cheap to verify and others are speculative.** A finding that can be checked quickly is worth more than an unverifiable opinion (UX feedback without usage data is near-worthless). Static analysis is free to run and only ambiguous output needs model tokens.
- **Different kinds of work need different cadences.** Some make sense at every run, some periodically (an architecture review is expensive and may only be worth it quarterly), some only when something happens (docs matter where code changed).
- **Not every kind of work applies to every repo.** Tests do not exist everywhere, dependency review needs a manifest, and some repos have kinds of work of their own. **Unknown:** who should decide what applies (an LLM, the human, configuration).
- **Some work may depend on the output of other work.** **Unknown:** whether such dependencies exist and would matter.
- **Repeating the same work has diminishing returns.** Re-running a task whenever budget exists wastes quota. How stale a task is depends on time since its last run and on how much relevant change happened since, but relevance is hard to measure (which files matter for which kind of work) and changes prompted by the system's own last report would look like fresh change, a self-reinforcing loop. **Unknown:** whether a run with different AI parameters (Sonnet earlier, Opus now) counts as the same work.
- **The set of repos changes over time.** New repos appear and renames are in progress (`claude-*` to `agent-*`), so any survey (last done 2026-08-23) goes stale.
- **The usefulness of the output is unproven.** Nothing shows yet that generated reviews are worth reading. The biggest uncertainty is content value, not scheduling, and a review nobody reads has zero value.

## 10. AI parameters and the risk of being wrong

- **Mistakes cost more in some places.** A wrong or missed finding in a trading bot or a live deployment costs more than in an archive, and every false positive costs reviewer time.
- **Stronger models and higher effort may produce better work.** More capable settings could find more or judge better on the same task, but the gain on maintenance tasks is untested.
- **Surplus quota changes what is affordable.** Models and effort levels the user normally avoids become reasonable because the quota would otherwise be wasted.
- **Stronger settings burn quota faster.** That could help reach a waste-free floor sooner, but the meter tracks list-price dollars closely within a window (cache reads weigh about 4–5× less than list price), so a costlier model (Opus 5 = 2.5× Sonnet 5 = 5× Haiku 4.5) exhausts a band sooner, and a task too big for the band cannot run.
- **The effect of effort level is unmeasured.** Every logged event so far is high effort, so there is no cost coefficient for lower levels.
- **The agent is itself a parameter.** Claude and Codex have separate quotas, separate model lineups and different cost profiles (§15).

## 11. Cost and duration of a task

- **Dollars and wall-clock time are different quantities.** A token-cheap task can be slow (waiting on tests, tools, network) and a token-expensive one fast, so knowing one says little about the other.
- **There is no history to estimate from yet.** Predictions can only come from past similar tasks, and none exist.
- **The meter is a rough yardstick early in a window.** The 5-hour budget varies about ±30% across windows (§5), although the meter is linear in cumulative list-price dollars within a window, so a task's cost is hard to read off the meter at the start of a window.
- **Some tasks are open-ended.** "Run the linter and summarize" is well bounded; "find the root cause of an intermittent race" can take arbitrarily long however cheap it looks on paper.
- **Finding out where work is needed costs quota too.** Deciding whether a repo deserves attention is itself work, and the overhead grows with the number of repos.

## 12. Combining considerations into a choice

- **Value, risk and cost share no common unit.** Value has no natural unit at all. **Unknown:** how to merge them into one ranking.
- **There are two budgets, not one.** A batch of work can fit the dollar band and still not fit the time left before the deadline.
- **The timing of uncertain tasks matters.** The cost of a batch is uncertain, and landing close to a target is like filling a 1L container with slots of unknown size. An overrun early in the period can be absorbed by later windows, while the same overrun in the last window cannot.
- **Small tasks cost more per unit of work.** Each task has fixed startup and context-loading costs, while coarse tasks land less precisely on a target.

## 13. Timing of execution

- **Running past the 7-day reset is a cost, not a bonus.** A task that ends after the reset draws its post-reset part from the fresh period, right when human use is often heaviest, and it did not rescue the expiring quota.
- **A weekly reset does not clear the 5-hour window.** Extra work that saturates a 5-hour window shortly before the reset leaves it saturated after the reset, even though the week is fresh, so the user could be blocked for up to 5 hours right at the start of the new week. Spend after the reset in that same window is charged to the new week.
- **Running past a 5-hour boundary is different.** It uses up the next window's headroom, but it can also be what starts the next window (§8).
- **Stopping a task is not free.** Killing an agent mid-task wastes the tokens already spent and loses partial findings, and stopping gracefully would need the agent's cooperation. **Unknown:** what each agent's CLI supports.
- **An unattended system needs a cadence, and the right cadence differs by job.** Checking whether anything should happen is cheap and can be frequent; running work is expensive and long; opening a window is instantaneous but must happen at a precise moment.
- **Two runs must not overlap.** A job that starts while the previous one is still working would double-spend the same budget (§14), so unattended runs need a way to know that one is already in flight.
- **A missed moment cannot be recovered.** If the machine is asleep or busy when a window should have been opened or a budget spent, that opportunity is gone, so the system has to notice and re-plan rather than assume its schedule ran.
- **Readings are stale between observations.** The closer the deadline, the more a stale reading can cost. Observing is almost free on Claude (pushed per message, §15) and costs a subprocess on Codex. Task durations are comparable to the interval between readings, so a running task is often still in flight when the next reading arrives.

## 14. Concurrency

- **Parallel work cannot raise the ceiling, only reach it faster.** The 5-hour cap is almost certainly account-wide within an agent, so running more agents at once only shortens the wall-clock time needed, which matters only when time is short.
- **Parallel work adds risk before it adds value.** Races on shared state or output, and harder debugging. The risk depends on whether tasks share a write target (same repo, same state file).
- **Work in flight is invisible to the readings.** A budget computed from readings that do not yet include running tasks would be spent twice by an overlapping run.
- **Extra work and interactive sessions share the same account.** Heavy background usage could slow down or throttle the user's interactive sessions before any quota is saturated. **Unknown:** whether such account-level limits (rate limits, concurrency) exist and how they behave.
- **Two agents mean two independent pools.** Claude and Codex run as separate processes with separate quotas, so they share neither quota nor writes unless they target the same repo. **Unknown:** whether "one at a time" should apply globally or per agent.

## 15. Agent differences (Claude vs Codex)

- **Same shape, different rules and scales.** Both expose a 5-hour and a 7-day meter (Codex: 300 and 10,080 minutes), but they are *not* normalised into one schema: the recorded history keeps each source's raw shape, and even the two latest-reading state files differ in field count, reset format and owning project. Normalising them is this stage's job, not upstream's. The table of §5 shows how the window rules and dollar scales differ, and in particular the fixed-deadline picture of §2 holds only for Claude.
- **Freshness differs.** Claude pushes live `rate_limits` on every message at no network cost. Codex has no equivalent: readings come from JSON-RPC polling (60 s when watched, 300 s idle) or the freshest local session file, so they are coarser and staler.
- **Unattended launch differs.** Claude has headless `claude -p`. **Unknown:** Codex's equivalent.
- **Safety enforcement differs.** "Read-only" is one requirement, but the permission and sandbox flags that enforce it are specific to each CLI and each would need its own verification.
- **Cost profiles differ.** Claude's model price ratios are known (§10). Codex's USD scale depends on which OpenAI list price is taken as the reference, and part of its usage is invisible locally (§5).

## 16. Safety and coexistence

- **Write access enlarges the blast radius.** Unattended agents that can commit, push or edit could do damage that nobody sees until later.
- **Even read-only agents can leak secrets.** They still read keys (the trading-bot repos hold them) and could quote them in reports or logs.
- **Unvalidated findings would compound errors if acted upon.** Report quality is unproven (§9), so acting automatically on findings could multiply mistakes.
- **Another unattended writer already exists.** `auto-commit` schedules commits into some repos for GitHub-activity presentation. Two unattended writers in one repo without coordination could mean races and a tangled history.
- **Unattended jobs can fail silently.** A sleeping machine or a crashed agent may go unnoticed, and a run that dies mid-task has already spent tokens.

## 17. Outputs and persistence

- **Reviewer time is limited.** Results that are slow to read have little value however good the analysis, so how quickly a human can digest them matters.
- **Findings recur across runs.** Without a way to recognise a finding seen before, the user re-reads the same thing each run and earlier decisions (accepted, rejected, resolved) are lost, which also feeds the staleness question of §9.
- **Where bot-authored artifacts live has trade-offs.** Inside the reviewed repos they enter project git history and could collide with `auto-commit` or expose secrets. Scattered across repos they make a cross-repo view hard. A separate location avoids both but is one more place to maintain.

## 18. Environment and prior art

- **Native scheduling exists.** Anthropic's Scheduled Tasks (`/routines`) are cloud-executed and cron-like, which would also avoid the "machine asleep" failure mode of a local scheduler. **Unknown:** whether they support usage-based triggering or only wall-clock cron. If only cron, they could cover execution but not a quota-aware trigger.
- **Existing monitors predict usage.** `Claude-Code-Usage-Monitor` and similar statusline projects already do. No prior art was found that combines usage prediction with scheduled maintenance to drain wasted quota, which appears to be the novel part.

## 19. Auto-resuming quota-paused sessions (separate project)

- **The concept.** A session stopped by a saturated 5-hour window sits idle until the user comes back, possibly hours after the reset. Resuming it automatically at the reset recovers that human work and starts the next window without the idle gap described in §8.
- **Why a separate project.** It acts on the user's own sessions (human work), not on extra work. It is small, useful on its own, and can ship long before this project. It lives in `~/dev/agent-auto-resume`.
- **Why it still matters here.** A queued resume is predictable human usage at the start of the next window (the best forecast input §7 can get). It already triggers the next window, so this project only needs to do so when nothing is queued. Extra work must not be scheduled into a window a resumed session is about to consume. The interface between the two projects is the pending-resume queue file written by `agent-auto-resume`.
