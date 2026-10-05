# 05 — Planning (`aqm plan`)

Stage 5 turns the budget into the **execution mandate**: for each agent, how much may still be spent, until when, **from when**, and an ordered queue of candidate tasks. It is the only thing `aqm execute` reads, so the supervisor needs no arguments.

| | |
|---|---|
| Command | `aqm plan [--budget PATH] [--out PATH]` |
| Reads | `data/<agent>/slots.csv`, the latest budget (`../03_budgeting/DESIGN.md` §3), the latest prediction, `config.json` |
| Writes | `artifacts/plans/<date>/<time>.json` |
| Acts | No |
| Consumed by | `../06_execution/DESIGN.md` |

Budgeting answers *how much and by when*; this stage answers *from when, what, where and with which AI parameters*, and combines both into numbers the executor can act on directly.

## 1. Lead time and execution profiles

This is the only decision that ever puts the system in the user's way, and the only place `QUIET_HOURS` is used.

```text
lead_time  = amount / intended_rate × SAFETY
start_after = deadline − lead_time
```

`intended_rate` is **a policy choice, not a forecast**: it says how hard the system intends to burn, and therefore how early it must start. `QUIET_HOURS` selects one of two profiles, which set the rate, the parallelism and the model preference together:

| Profile    | When                                     | `intended_rate`          | Workers                        | Models                 | Lead time for 0.9 unit            | Behaviour                                                                   |
| ---------- | ---------------------------------------- | ------------------------ | ------------------------------ | ---------------------- | --------------------------------- | --------------------------------------------------------------------------- |
| **gentle** | Inside `QUIET_HOURS` (00:00–09:00 local) | `GENTLE_RATE` 0.2 unit/h | 1                              | Cheap, long tasks      | 6h45 — longer than a whole window | Starts as soon as a window has an amount due, and trickles through the night |
| **burst**  | Any other hour                           | `BOT_BURN_UNITS_PER_HOUR` 1.0 unit/h  | Up to `MAX_PARALLEL_PER_AGENT` | Expensive, short tasks | 1h20                              | Holds back, then burns hard near the end of the window                       |

Why the two differ: during the user's hours a collision must be **short**, so the system waits and then burns fast — the worst case is being in the way until the window ends. At night there is nothing to collide with, so a slow cheap trickle is better: it leaves time to notice problems, and cheap models read more code per unit spent. Converting a bounded 5-hour risk into an unbounded 7-day one is exactly what this design refuses to do (`../03_budgeting/PREVIOUS_IDEAS.md` §4.5).

`QUIET_HOURS` is not a claim about the user being asleep; it is a statement about which risk is acceptable in those hours. It is static configuration in the MVP, and the single place a learned activity profile would plug in (`../02_prediction/PREVIOUS_IDEAS.md`).

**Once the burn rate is measured**, `intended_rate = min(profile rate, measured rate × MAX_PARALLEL_PER_AGENT)`. It is measured by the executor, one sample per worker in `state/burnrate.jsonl` (`../06_execution/DESIGN.md` §3). Until `MIN_BURN_SAMPLES` exist, the profile rates above are used as-is, which errs towards leaving quota unspent rather than starting too early. If the machine cannot burn as fast as the profile assumes, the lead time grows and the start moves earlier by itself — the system never promises a burn it cannot deliver.

## 2. Output: the execution mandate

```json
{"computed_ts": 1790170589, "config_hash": "9f2c…",
 "agents": {
   "claude": {"amount": 0.30, "spend_by_ts": 1790188200, "start_after": 1790186400,
              "profile": "gentle", "max_parallel": 1,
              "queue": [
                {"repo": "~/dev/agent-statusline", "category": "bugs", "rank": 3.2,
                 "model": "sonnet", "effort": "high", "estimate_units": 0.25, "estimate_minutes": 12,
                 "why": {"repo_weight": 1.0, "category_weight": 1.0, "staleness_days": 3.2}}]},
   "codex":  {"amount": 0.0, "spend_by_ts": null, "start_after": null,
              "profile": "gentle", "max_parallel": 1, "queue": []}}}
```

| Field | Where it comes from |
|---|---|
| `amount` | `budget.extra_quota_to_spend_units`, clamped by the room left in the window (§3) and by `MAX_DAILY_UNITS` minus what was already spent today |
| `spend_by_ts` | Straight from the budget (`../03_budgeting/DESIGN.md` §2) |
| `start_after` | `deadline − lead_time(amount)` (§1): the moment work should begin, which is what makes the system hold back during the user's hours and trickle at night |
| `profile`, `max_parallel` | The execution profile of §1, from `QUIET_HOURS` |
| `queue` | Ranked candidates, §4 |

An agent with `amount = 0` has nothing to run; `spend_by_ts` and `start_after` are then `null`. The queue is candidates, not commitments: the executor pops from it, skips repos whose lock is held, and stops whenever budget or deadline says so. Re-running `aqm plan` after a run reflects the new staleness.

## 3. The room test

```text
room = 1 − window_used − prediction.window.p95
amount = min(budget.extra_quota_to_spend_units, room, MAX_DAILY_UNITS − spent_today)
```

This is the general form of what would otherwise be a yes/no veto: a user spending right now produces a `p95` close to a full window, which leaves no room and keeps that agent out of the plan entirely, while a user who sent one small message does not. With a real predictor the same line also holds back work before a user who is merely *likely* to arrive.

In the MVP this is the **only** place room is computed; the executor works to the frozen number and does not re-check it (`../06_execution/DESIGN.md` §5).

## 4. The queue

| Queue decision | How |
|---|---|
| **Which repo, which category** | `rank = weight(repo) × weight(category) × staleness(pair)`, staleness growing with the time since that pair last ran (`state/pairs.json`) |
| **Which agent** | One queue per agent, since each has its own budget; the same (repo, category) can appear in both, and whichever agent reaches it first takes the repo lock, so it runs once (`../06_execution/DESIGN.md` §2) |
| **Which model and effort** | From the profile — cheap and long inside quiet hours, expensive and short outside — as a default the executor may escalate from |
| **How big** | `estimate_units` from past runs of the same (category, model), used for the reservation of `../06_execution/DESIGN.md` §3 |

**Categories.** Three to start, each with its own prompt and cadence:

| Category     | Output                                             | Cadence                  |
| ------------ | -------------------------------------------------- | ------------------------ |
| `bugs`       | Suspected defects: file, line, failure scenario    | Every run (top priority) |
| `agent-docs` | Drift between `CLAUDE.md`/`AGENTS.md` and the repo | Weekly                   |
| `tests`      | Missing or weak coverage on recently changed code  | Weekly                   |

**Prompts.** Each category has a template in `prompts/<category>.md`, rendered with the repo path. Every prompt ends with the same three instructions: produce the Markdown report inline in the final message, never modify anything, never quote credentials. The executor writes the file itself — the model has no write tool.

## 5. Reports

```markdown
---
repo: agent-statusline
category: bugs
model: claude-sonnet-5
effort: high
cost_usd: 0.84
duration_s: 412
session_id: 0b6f...
finding_hashes: [a91c..., 7f20...]
---
## Findings
### 1. <title>  ·  `src/foo.py:120`
**What breaks:** ...
```

- **Location:** `~/opt/agent-quota-maximizer/reports/<YYYY-MM-DD>/<repo>__<category>.md`, outside every reviewed repo, so nothing collides with `auto-commit` and no bot artifact enters project history (`../CONSIDERATIONS.md` §17).
- **Index:** `reports/index.md` is rewritten after each report, so no separate job is needed.
- **Finding hashes:** repo + file + normalised title, so repeats can be recognised later.
- **Secret scrubbing:** a regex pass (API-key shapes, `Authorization:` headers, `.env`-style assignments) before writing; matches are replaced and counted in the run record.
- **Retention:** reports older than `REPORT_RETENTION_DAYS` are deleted during housekeeping.

## 6. Testing

Unit tests on ranking, staleness, model and effort by profile, and estimates. Acceptance for P5: reports are produced nightly and readable in a few minutes.

## 7. Open points

- **Task usefulness is unproven** (`../CONSIDERATIONS.md` §9): P5 is judged on whether the reports get read.
- **Findings memory** across runs: hashes are written from P5, but deduplication and a "seen before" view are not designed.
- **Tasks are lumpy.** `estimate_units` is a median, so the last task of a mandate can overshoot or leave a remainder below `MIN_TASK_BUDGET`; splitting a category into smaller units is not designed.
- **The repo list and its weights** are still owed by the user, along with any repo to exclude for secrets — a prerequisite of P5.
