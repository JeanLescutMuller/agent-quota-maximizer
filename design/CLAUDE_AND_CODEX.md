# Claude and Codex — measured data, mechanics and consequences

The two agents this system manages. They have the same shape — a 5-hour meter nested in a 7-day one, both use-it-or-lose-it — and differ in almost every detail that matters: when a period starts, how fresh a reading is, how much is visible locally, and how read-only is enforced. This file is the per-agent reference; the rules that are the same for both live in the stage designs.

**Field-level detail — which source reports percent, tokens or USD, with exact paths and every trap — is canonical in `~/dev/agent-usage-tracker/USAGE_DATA_SOURCES.md` (what exists upstream) and `~/dev/agent-usage-tracker/USAGE_DATA_REFERENCE.md` (what is captured, and where).** This file is the design-facing summary: what the differences *mean* for this project. It does not restate the schemas.

Everything below was measured on this machine **[verified]** or comes from `~/dev/agent-usage-tracker/adhoc_quotas_analysis/CONCLUSIONS.md`. Figures marked **[derived]** are arithmetic on those measurements, not observations.

## 1. What the data says

Sources: 155 Claude transcript files (~24,000 priced messages) and 110,795 quota rows; 66 Codex session files (28 with usage events, 2,452 deduped `token_count` events, 2026-08-27 → 2026-09-16) and 3,810 poller snapshots.

| | Claude (Pro) | Codex (Plus) |
|---|---|---|
| Weekly capacity | ≈ 8.85 five-hour windows ≈ **$283** — but drifting: 10.1 → 7.6 over five weeks, so measured since 2026-10-06 (`03_budgeting/DESIGN.md` §2) | ≈ 6.2 five-hour windows ≈ **$127** — weakly measured, the feed is sparse |
| 100% of one 5-hour window | $32 (middle 80%: $20–39) | $20.5 (range $16.7–26.3) |
| **Weekly peaks observed** | **81%, 88%, 10%, 27%, 47%** | **46%, 36%, 30%, 2%, 0%** |
| Mean weekly peak | ≈ 51% **[derived]** | ≈ 23% **[derived]** |
| Weekly waste, in dollars | ≈ **$139/week** **[derived]** | ≈ **$98/week** **[derived]** |
| 5-hour windows seen | 35, of which **5 reached 100%** (≈ 4 h at the cap over 30 days) | 30, of which **1 reached 100%**; median peak 5% |
| Idle gaps between windows | 26 of 31 pairs (median 11.5 h); windows cover only **24%** of elapsed time | 21 of 28 pairs (median 10.0 h) |
| Daily spend when used | Mon $38, Tue $16, Wed $23, Thu $16, Fri $10, Sat $6, Sun $27; quiet 23:00–06:00 UTC | $43, $32, $33, $21, $18, $10, $16 |
| Days with any usage | 19 of 29 | — |
| Last usage | Current | **2026-09-16** — 7 days ago; the current period shows 0–2% used, reset 09-28 |
| Models seen | opus, sonnet, haiku | `gpt-5.6-sol` (2,286 events), `codex-auto-review` (166) |

**Neither agent is the afterthought, and for opposite reasons.** Codex wastes a far larger *share* — more than half of its week routinely, and entire weeks completely. Claude wastes a smaller share of a much larger allowance, which in absolute money is the **bigger** loss: roughly $139 a week against $98. The pipeline therefore plans both independently from day one rather than building one and adding the other.

Only five weeks of data, and both columns understate demand: when a window saturates, what the user actually wanted is unrecorded (`CONSIDERATIONS.md` §7).

## 2. Mechanics, side by side

| Mechanic | Claude | Codex | Consequence for this system |
|---|---|---|---|
| 5-hour window | `rate_limits.primary`; opens on the first message after the previous expired | Same, 300 min | Identical handling; the window starter is agent-agnostic here |
| 7-day period | **Fixed: every Monday 19:00 UTC** | `rate_limits.secondary`, 10,080 min, **starts at the first message after idle** | On Codex, idle time is not banked: the later a week starts, the fewer weeks per year |
| Reset times | Exact and known in advance | Drift, and can be pulled forward by the server: observed 09-07 19:20, 09-15 09:14, 09-19 11:24, then 09-28 04:13 **and** 09:05 for the same period **[verified]** | No Codex schedule can be precomputed; the meter is re-read every tick for both |
| Does a 7-day reset also reset the 5-hour window? | No: the running window keeps its % and its end (seen twice) | No at a natural expiry (seen once, at 1–2%); **yes** at the 08-27 server-side early reset, which re-anchored both | The window split at the reset is correct for both (`03_budgeting/DESIGN.md` §2) |
| Idle reading | `null`, 0% | A fake countdown: `used_percent == 0` with `resets_at ≈ now + span` | Codex's must be dropped, or the system believes a fresh window is open (`01_ingestion/DESIGN.md` §5) |
| Meter freshness | **Free live push**: every Claude Code render carries `rate_limits` on its own `/v1/messages` response | No equivalent. Two feeds: `token_count` events inside a running session (exact, free) and the poller (~5 min) | Between Codex sessions the meter is up to 5 minutes stale; `READING_MAX_AGE_SECONDS` is sized for the worse of the two |
| Usage visible locally | Complete: every % step since 09-13 matched a local message | Only what this machine ran; cloud, IDE and ChatGPT-app usage raise the meter with no local trace | Spend attribution is weaker on Codex; signal S3 exists for exactly this (`02_prediction/DESIGN.md` §3) |
| Pricing | Known and validated: opus 5/25, sonnet 2/10, haiku 1/5 per MTok; the 2.5× Opus/Sonnet ratio matches the meter | **Assumed**: `gpt-5.6-sol` list price 5/30 per MTok | Codex dollars are indicative; percentages are safe on both, which is why the budget is enforced on the meter |
| USD reported by the agent | Only from `claude -p`, and at **list** basis (`costBasis: "list"`) **[verified]** | **None at all** | No agent reports subscription-basis dollars; USD is never a budget |
| Percent resolution | Whole percent at the source — the `/v1/messages` headers carry two decimals of a fraction **[verified]** → 1% ≈ $0.32 | Integer-valued in every row observed **[verified]** → 1% ≈ $0.21 | Neither quantum is recoverable from the live feeds; Codex's `plan_limit_history` has sub-percent history, not yet captured (`USAGE_DATA_REFERENCE.md` §5.1, `USAGE_DATA_SOURCES.md` §4.4) |
| Account-wide token total | None | **`codex_usage.dailyUsageBuckets`**, 37 daily totals **[verified]** | The only signal on either agent that sees cloud, IDE and app usage — daily granularity |
| Unattended launch | `claude -p … --permission-mode plan --disallowedTools …` **[verified flags]** | `codex exec -s read-only -C <dir> …` **[verified flags]** | Read-only is a sandbox mode on Codex, stronger than Claude's flag combination |

## 3. Reading the meter

| | Claude | Codex |
|---|---|---|
| Primary feed | Statusline push into `data/claude/account.jsonl`, at no network cost | `token_count` events in `~/.codex/sessions/**/*.jsonl` (exact, only while a session runs) |
| Fallback feed | A scheduled poller — but **8,516 of its 12,848 rows carry a real error** (SSL, HTTP 429, missing token) **[verified]**, so the push path does ~92% of the work | The poller in `data/codex/account.jsonl` (~5 min), sharing that transport |
| Trap | Idle renderers re-push day-old readings, so line order lies | The fake idle countdown looks like a fresh empty window, and its reset time moves every tick |
| Readings surviving the drop_stale_readings | **23/day** **[verified 2026-10-04]** | **4.6/day** — five times sparser, and sparsest while a session is running, because the poller stands down then **[verified]** |
| Reset time, as recorded | Epoch-seconds *string* on push rows, ISO on poller rows | Epoch integer, drifting ±1 s between readings of one window **[verified]** |

The traps are defeated by three rules, which is why they live in ingestion and not here: readings are keyed by `observed_ts`; within a window only the first reading of each new maximum is kept; and the window key is a reset time rounded to the minute, after fake countdowns have been dropped (`01_ingestion/DESIGN.md` §5).

## 4. Telling human work from our own

**No meter reading carries a session id on either agent, and none can** — the meter is account-level (`USAGE_DATA_SOURCES.md` §2.3). Attribution is therefore done by **time interval**, not by session: the executor records when its own work ran, and everything else that moved the meter was the user (`01_ingestion/DESIGN.md` §1). That is agent-agnostic, and it is the one attribution method that also catches Codex usage from clients this machine cannot see.

The per-agent session markers still exist, and are what a finer future attribution would use:

| Agent | Marker | Cross-check |
|---|---|---|
| Claude | The executor generates the session UUID and passes `--session-id`; the `.jsonl` transcript is `<uuid>.jsonl` **[verified]** | Two more, in the recorded data itself: our requests carry `query_source: "sdk"` in telemetry, and a headless run writes no status-line row at all **[verified 2026-10-04]** |
| Codex | `codex exec -C <scratch dir>` **[verified]**; `session_meta` carries that `cwd`, and the executor records the rollout file that appears | Our work always runs with our model and effort settings |

**The asymmetry is now larger, not smaller.** On Claude, both halves of an overlapping interval are measurable in dollars per request: the user's from status-line rows and non-`sdk` telemetry, ours from `sdk` telemetry — and because headless runs never render, the push feed is human-only by construction (`USAGE_DATA_REFERENCE.md` §1, §2.1, §9). On Codex there is **no USD on any channel and no session scope at all**, so an overlapping interval can only ever be bounded, never apportioned. Claude **hooks** carry no usage data either **[docs]**.

Ranked by reliability, the signals that the *user* is spending differ by agent, and the cheap one that looked agent-agnostic turned out not to be a signal at all. Meter movement we did not cause works on both. Beyond that, Claude has two account-side confirmations — a new push row, and a non-`sdk` telemetry row — while Codex has only its own session files, which must be **tailed** rather than stated, because a file's mtime moves without anything happening in it (`01_ingestion/DESIGN.md` §3.1). The latency of the first signal also differs sharply: 23 meter readings a day on Claude against 4.6 on Codex (`02_prediction/DESIGN.md` §3).

Anything unclassifiable counts as human on both — the conservative direction.

## 5. Launching extra work

```text
Claude: claude -p <prompt> --session-id <uuid> --model <m> --effort <e>
            --output-format json --permission-mode plan
            --disallowedTools "Write,Edit,NotebookEdit,Bash,WebFetch,WebSearch"
            --add-dir <repo>                                          [verified flags]
Codex:  codex exec <prompt> -s read-only -C <scratch dir> -m <model>
            -c model_reasoning_effort=<effort> --json -o <file>       [verified flags]
```

Cost is read from each agent's own accounting — Claude's `total_cost_usd` **[verified: $0.0201843 on the test call]**, Codex's `token_count` events — and recorded for comparing models. Neither is the budget: **the budget is enforced on the meter** on both agents, because the dollars-per-percent ratio varies about ±30% between windows while the percentage does not (`06_execution/DESIGN.md` §3).

## 6. Design consequences

### 6.1 The window starter matters more on Codex

On Claude, opening a window only rescues quota near the reset, because the weekly deadline is fixed whatever happens. On Codex it also decides **when the week starts**, and therefore how much quota the year contains: a period that begins two days late is two days of allowance that never existed.

```text
if no 7-day period is running (fake countdown seen) and the user has been idle:
    open one with a minimal message
```

This is the cheapest, highest-value action in the whole project: one message worth a few cents starts a $127 allowance. It is also nearly risk-free, since an unused period costs nothing. The rule is part of stage 4 (`04_start_windows/DESIGN.md` §1); the Claude rules are in `04_start_windows/CONSIDERATIONS.md`.

**Open question:** whether to start a Codex period immediately after a reset, or to wait for the user's own first message. Starting immediately maximises periods per year; waiting aligns each period with the user's rhythm. With a fixed 7-day span the two only differ by the idle delay, so starting immediately is preferred, and the risk is limited to the period's end landing at an awkward hour.

### 6.2 The budget arithmetic is agent-agnostic

The back-fill needs only the remaining percentage and the two reset times, both read from the meter (`03_budgeting/DESIGN.md` §2). Nothing in it assumes a weekday or an hour, so Claude's fixed Monday 19:00 UTC reset and a Codex reset at 04:13 on a Sunday are handled by the same code. Per-agent parameters differ only in how `CEILING` is interpreted (percent of that agent's own window) and in the burn rate, which is measured separately.

The one asymmetry is confidence in the deadline: Claude's is exact, Codex's is unknown until the first message of the week and can be pulled forward without warning. A Codex plan computed just before an early server reset is simply wrong until the next tick — which is survivable only because every tick re-plans from live readings.

### 6.3 Two agents, one machine

- **Separate state per agent** (`data/<agent>/slots.csv` holds an `agent` column, and the meter file is per agent), one artifact per tick with a section per agent, separate budgets, separate parameters, separate daily caps.
- **One supervisor covers both**, since their quotas are independent and the plan carries both mandates. What they share is the repo locks and the global worker cap (`06_execution/DESIGN.md` §2).
- **The "user is active" check is per agent** (`05_planning/DESIGN.md` §3; the room test until 2026-10-06). The user working in Claude does not keep Codex extra work out of the plan, and vice versa — they are separate quotas — but a shared machine means both compete for CPU, which `MAX_PARALLEL_TOTAL` bounds.
- **Waste is measured per agent**, and the notebook reports both (`07_pipeline/DESIGN.md` §8).

## 7. What is still unknown

| | Question |
|---|---|
| Codex | Whether `codex exec` reliably opens a window the way an interactive message does, and how quickly the meter reflects it — to verify at P3 |
| Codex | Whether a period can be started and then left unused with no penalty — expected, but unverified over a full week |
| Codex | Whether `codex-auto-review`, which appears as a separate model in the event stream, bills to the same meter |
| Codex | The real dollar scale, which depends on which OpenAI list price is the reference |
| Claude | The exact read-only flag set for the installed CLI version, to re-verify at P4 |
| Claude | How complete the telemetry feed is in steady state. It is the only per-request cost record, but events are dropped when the receiver is down and nothing is buffered on disk — 66 rows over the first 4 days is too little to judge **[verified 2026-10-04]** |
| Codex | Whether `plan_limit_history` eventually covers every finished window, which would make it a usable backtest ground truth (`02_prediction/DESIGN.md` §6). Only 4 fetches so far |
| Both | Early server resets (seen on Codex 08-27, 08-31, 09-12) can re-anchor both meters at once, including the 5-hour window |
| Both | Whether the cheapest possible message reliably opens a window, and what that message is |
| Both | Account-level throttling with several workers in flight |

A third agent is out of scope (`DESIGN_v2.md` §2); adding one would mean generalising this file rather than extending the stage designs, which already treat the agent as a parameter.
