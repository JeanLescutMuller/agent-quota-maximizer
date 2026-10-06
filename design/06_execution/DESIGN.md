# 06 — Execution (`aqm execute`)

Stage 6, and the only one that spends real quota. A **supervisor with a worker pool**, one process covering both agents: it reads the latest plan and runs each agent's mandate against that agent's own quota. Parallelism raises the burn rate without raising the ceiling, which is what buys back lead time and therefore keeps the system out of the user's way (`../CONSIDERATIONS.md` §14.1).

| | |
|---|---|
| Command | `aqm execute [--plan PATH] [--max-parallel K] [--force]` |
| Reads | The latest plan, once (`../05_planning/DESIGN.md` §2) |
| Writes | Reports, `state/runs.jsonl`, `state/burnrate.jsonl`, `state/pairs.json` |
| Acts | **Yes**, up to `amount` per agent |

**Revised 2026-10-06** (`../DESIGN_v2.md` §8): it runs on the Debian VM; its first deliverable is the measured burn rate, which decides whether the no-forecast rule is enough; and `GUARD_PCT` is gone — making sure each task fits what it was given is this stage's own concern, handled by the reservation of §3, not a separate parameter.

It takes no agent, amount or deadline: the plan already carries the mandate for every agent, so the supervisor has nothing to be told. Forcing a single task needs no flag — a one-line queue in `--plan` does it through the real code path. The pipeline spawns it detached (`Popen(start_new_session=True)`, output to `logs/executor.log`), so launchd never waits on a task.

## 1. The loop

```text
plan = latest plan artifact
for each agent in plan with amount ≥ MIN_TASK_BUDGET and now ≥ start_after:
    reserved[agent] = 0                        # units promised to that agent's running workers

while any agent still has work and now < its deadline:
    reload config; if not enabled:                     stop all "disabled"
    # whether the user was active was checked at plan time, not re-checked during the run (§5)
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

**The first thing this stage delivers is the burn rate**, before any of the rest is tuned: per agent, with 1, 2 and 3 parallel workers, in units per hour of meter movement. It decides the design (`../DESIGN_v2.md` §8.2): at 0.5 unit/h or more the no-forecast rule stands; below, more workers first, and if that is still not enough, prediction comes back.

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
| Deadline reached                                         | Stop launching; running workers get `GRACE`, then are terminated                                                                                 |
| Budget exhausted                                         | Stop launching; running workers finish                                                                                                          |
| Config disabled                                          | Stop launching and terminate all workers                                                                                                        |
| Worker timeout (`TASK_TIMEOUT`)                          | Terminated; its partial spend still shows in the meter delta                                                                                    |
| Two consecutive worker failures                          | Supervisor stops and records `last_error`                                                                                                       |

## 5. Deferred: noticing a user who arrives during a run

The supervisor works to the frozen `amount`. Planning checks that the user is not active before anything starts (`../05_planning/DESIGN.md` §3), but a user who starts working two minutes into a twenty-minute task is not noticed until the run ends.

What bounds the damage meanwhile:

| Bound | Effect |
|---|---|
| `MIN_HUMAN_RESERVE_PCT` is never filled | Whatever happens, a slice of the window is left for the user |
| `spend_by_ts` | The run cannot outlive the window it was planned for |
| Late start | During the user's hours work begins as late as the burn rate allows, so the exposure window is short by construction (`../05_planning/DESIGN.md` §1) |
| `TASK_TIMEOUT` | No single worker runs longer than 20 minutes |

**Why it is harder on the VM.** While the bot runs, the meter moves for the bot and the user alike, and the VM cannot see the Mac's sessions. Three ways to notice the user, none chosen yet:

| Way | Cost |
|---|---|
| Between two tasks, wait one meter poll with nothing running: any movement is the user | Throughput: a few idle minutes per task |
| Compare the meter's movement with what the bot's own tasks cost, converted at the measured rate | The conversion varies ±30% between windows, so only a large excess is a reliable signal |
| The Mac tells the VM: a status-line render means the user sent a prompt | Couples the two machines; nothing is heard while the Mac sleeps, which is also when the user is not at it |

How much it matters depends on the burn rate: at 0.5 unit/h or more the replay found no collision without it (`../DESIGN_v2.md` §8.2). Until 2026-10-06 the deferred feature was a live re-read of the forecast; with the forecast parked, it is this.

The metric that decides whether it is worth building is **near misses** — runs where the user became active while workers were still running (`../07_pipeline/DESIGN.md` §8).

## 6. Testing

Concurrency tests: two supervisors competing for one repo, the slot semaphore at its cap, a worker killed mid-task. Acceptance for P4: budget respected within 1% of the meter with workers overlapping; a second task on a busy repo never starts; a task whose estimate exceeds what is left of `amount` never starts; the burn rate is measured per agent with 1, 2 and 3 workers.

## 7. Open points

- **Burn rate** is unknown until P4 measures it, per agent and per model — and it now decides whether prediction comes back (`../DESIGN_v2.md` §8.2).
- **Account-level throttling** with several workers is unverified; `MAX_PARALLEL_PER_AGENT` rises only after measuring whether per-worker throughput degrades.
- **Exact read-only flag set** for the installed Claude CLI version, to re-verify at P4.
- **Noticing a user who arrives during a run** (§5): deferred; needed only if the burn rate is low.
