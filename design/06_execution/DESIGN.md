# 06 — Execution (`aqm execute`)

Stage 6, and the only one that spends real quota. A **supervisor with a worker pool**, one process covering both agents: it reads the latest plan and runs each agent's mandate against that agent's own quota. Parallelism raises the burn rate without raising the ceiling, which is what buys back lead time and therefore keeps the system out of the user's way (`../CONSIDERATIONS.md` §14.1).

| | |
|---|---|
| Command | `aqm execute [--plan PATH] [--max-parallel K] [--force]` |
| Reads | The latest plan, once (`../05_planning/DESIGN.md` §2) |
| Writes | Reports, `state/runs.jsonl`, `state/burnrate.jsonl`, `state/pairs.json` |
| Acts | **Yes**, up to `amount` per agent |

It takes no agent, amount or deadline: the plan already carries the mandate for every agent, so the supervisor has nothing to be told. Forcing a single task needs no flag — a one-line queue in `--plan` does it through the real code path. The pipeline spawns it detached (`Popen(start_new_session=True)`, output to `logs/executor.log`), so launchd never waits on a task.

## 1. The loop

```text
plan = latest plan artifact
for each agent in plan with amount ≥ MIN_TASK_BUDGET and now ≥ start_after:
    reserved[agent] = 0                        # units promised to that agent's running workers

while any agent still has work and now < its deadline:
    reload config; if not enabled:                     stop all "disabled"
    # room was computed at plan time and is not re-checked during the run (§5)
    pick the agent with the largest remaining fraction of its amount
    free = amount − spent_so_far[agent] − reserved[agent]
    if free < MIN_TASK_BUDGET:                         that agent is done
    if workers[agent] ≥ parallelism_target(agent) or total workers ≥ MAX_PARALLEL_TOTAL:   wait
    task = pop from that agent's queue, skipping repos whose lock is held
    if none:                                           that agent is done
    take repo lock; reserved[agent] += estimate(task)
    spawn worker                                       # §2
on worker exit:
    reserved[agent] −= estimate(task); spent_so_far[agent] = that agent's meter delta since start (§3)
    release repo lock; write report; append run and burn-rate records
```

## 2. Concurrency and the worker command

| Rule                              | Value                    | Why                                                                                                                                                                                                                                                  |
| --------------------------------- | ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **One task per repo at any time** | Hard                     | Two workers reading and reporting on the same repo would duplicate findings, confuse staleness accounting and compete for the same files (`../CONSIDERATIONS.md` §14.2). The repo lock is held for the whole task, so the rule holds **across agents**. |
| Per agent                         | `MAX_PARALLEL_PER_AGENT` | Account-level throttling is unverified (`../CONSIDERATIONS.md` §14.4); start at 2 and raise once measured                                                                                                                                             |
| Across agents                     | `MAX_PARALLEL_TOTAL`     | One machine, shared CPU and disk                                                                                                                                                                                                                     |
| Both agents at once               | Allowed                  | Independent quotas; one supervisor interleaves them, capped by `MAX_PARALLEL_TOTAL`                                                                                                                                                                  |

```text
parallelism_target(agent) = clamp(ceil(required_rate(agent) / measured_rate_per_worker),
                                  profile minimum, plan.max_parallel for that agent)
```

Within a run, `required_rate = remaining / time_left` drives escalation: above `ESCALATE_RATE` the supervisor moves to the expensive model and more workers.

```text
Claude: claude -p <prompt> --session-id <uuid> --model <m> --effort <e>
            --output-format json --permission-mode plan
            --disallowedTools "Write,Edit,NotebookEdit,Bash,WebFetch,WebSearch"
            --add-dir <repo>                                          [verified flags]
Codex:  codex exec <prompt> -s read-only -C <scratch dir> -m <model>
            -c model_reasoning_effort=<effort> --json -o <file>       [verified flags]
```

On Codex, read-only is a sandbox mode rather than a list of forbidden tools, which is the stronger guarantee of the two. The session id and the scratch directory are what make our own spend identifiable later (`../01_ingestion/DESIGN.md` §6), so both are recorded **before** the process is launched.

## 3. Budget with several workers in flight

Overshoot risk multiplies with parallelism, so the budget is managed by **reservation**:

- A task is launched only if `amount − spent − reserved ≥ MIN_TASK_BUDGET`.
- `estimate(task)` starts as a conservative constant per model and becomes the median of past runs of the same (category, model) once `MIN_BURN_SAMPLES` exist.
- `spent_so_far` is the **aggregate meter movement** since the supervisor started, re-read at every worker exit and at most every `METER_POLL`. With workers overlapping, the meter cannot be split per task, so it governs the batch. It can be polluted by the user spending at the same time, which is acceptable: over-attributing spend to ourselves is the conservative direction.
- The budget is enforced on the **meter**, never on dollars. Each worker's own `total_cost_usd` is **list-price dollars** — the result object states `costBasis: "list"` **[verified]** — so it is a different currency from the subscription meter, with an exchange rate that varies ±30% between windows (`~/dev/agent-usage-tracker/USAGE_DATA_SOURCES.md` §1). It is recorded for the report header and for comparing models, never for the budget.
- **The result object is on stdout; its content is also in the transcript.** It carries `total_cost_usd`, a per-model `modelUsage` breakdown, `usage` token counts and `session_id`, but **no `rate_limits`**: the headless path reports cost, never quota. The object itself is not persisted, but the session's transcript holds the same per-message `usage` and a `cost-state` record with identical `totalCostUSD` and `modelUsage` **[verified]** — capturing stdout is the convenient path, not the only one (`USAGE_DATA_SOURCES.md` §3.3).

**Burn-rate samples**, one per worker in `state/burnrate.jsonl`, are measured two ways: **per worker** (cost ÷ duration, valid even when workers overlap) and **aggregate** (meter delta ÷ wall clock, with the number of workers running). `parallelism_target()` uses the per-worker figure; the aggregate validates it. Planning consumes the same samples to compute lead time (`../05_planning/DESIGN.md` §1).

## 4. Stopping

| Event                                                   | Behaviour                                                                                                                                        |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| The meter shows less than `GUARD_PCT` of the window left | Stop launching, and terminate the newest workers first, being the ones with the least sunk cost. This is the only in-run protection in the MVP: it reacts to the meter, not to a forecast (§5) |
| Deadline reached                                         | Stop launching; running workers get `GRACE`, then are terminated                                                                                 |
| Budget exhausted                                         | Stop launching; running workers finish                                                                                                          |
| Config disabled                                          | Stop launching and terminate all workers                                                                                                        |
| Worker timeout (`TASK_TIMEOUT`)                          | Terminated; its partial spend still shows in the meter delta                                                                                    |
| Two consecutive worker failures                          | Supervisor stops and records `last_error`                                                                                                       |

## 5. Deferred: live room re-check

In the MVP the supervisor never re-reads the prediction. The room left for the user (`1 − window_used − p95`) is computed by the plan stage and frozen into `amount`, so a user who starts working two minutes into a twenty-minute task is not noticed until the run ends.

What bounds the damage meanwhile:

| Bound | Effect |
|---|---|
| `MIN_MARGIN` is never filled | Whatever happens, a slice of the window is left for the user |
| `GUARD_PCT` on the meter | The one live check: when the window is nearly full, launching stops and the newest workers are killed |
| `deadline` | The run cannot outlive the chunk it was planned for |
| Late start | During the user's hours work begins as late as the burn rate allows, so the exposure window is short by construction (`../05_planning/DESIGN.md` §1) |
| `TASK_TIMEOUT` | No single worker runs longer than 20 minutes |

**The deferred feature:** `aqm execute --prediction PATH`, defaulting to `state/latest/prediction.json` re-read in the loop, so a fresh prediction written by any later tick reaches the running supervisor and stops it for that agent. The plan would still be read once — the mandate must not move — while the prediction deliberately would be a moving target. It is the single cheapest upgrade to user protection once the MVP runs, and it changes nothing else: the room expression and the stop rule already exist (`../05_planning/DESIGN.md` §3, §4 above).

The metric that decides whether it is worth building is **near misses** — runs where the user became active while workers were still running (`../07_pipeline/DESIGN.md` §8).

## 6. Testing

Concurrency tests: two supervisors competing for one repo, the slot semaphore at its cap, a worker killed mid-task. Acceptance for P4: budget respected within 1% of the meter with workers overlapping; a second task on a busy repo never starts; `GUARD_PCT` stops launches when the window fills.

## 7. Open points

- **Burn rate** is unknown until P4 measures it, per agent and per model.
- **Account-level throttling** with several workers is unverified; `MAX_PARALLEL_PER_AGENT` rises only after measuring whether per-worker throughput degrades.
- **Exact read-only flag set** for the installed Claude CLI version, to re-verify at P4.
- **Live room re-check** (§5): deferred, and the first upgrade to make once the MVP runs.
