# 07 — Pipeline and plumbing (`aqm pipeline`)

Everything that is not one stage's own business: the command interface, the guards, scheduling, locks, state, configuration, observability, deployment and the shared parameter table. `aqm pipeline` runs the six stages in order and is the only command in the LaunchAgent.

The stages themselves are designed in `../01_ingestion/`, `../02_prediction/`, `../03_budgeting/`, `../04_start_windows/`, `../05_planning/` and `../06_execution/`. Facts marked **[verified]** were checked on this machine on 2026-09-23.

## 1. Interface: commands, processes and files

The subcommands **are** the pipeline stages. Each is runnable on its own and writes its artifact, so any stage can be inspected without running the next.

### 1.1 Pipeline commands

Two different questions are answered by two different stages, and the words are kept apart everywhere: **budget** decides *how much may be spent and by when*, **plan** decides *what to run, on which repo, with which agent and AI parameters*.

| #   | Command | Question it answers | Reads | Writes | Acts | Design |
| --- | --- | --- | --- | --- | --- | --- |
| 1   | `aqm ingest`<br>`[--backfill]` | What happened? | The quota logs of §1.3, plus file mtimes | the state files | No | `../01_ingestion/DESIGN.md` |
| 2   | `aqm predict`<br>`[--at <iso>]`<br>`[--out PATH]` | How much will the user still want? | the state files | the prediction artifact | No | `../02_prediction/DESIGN.md` |
| 3   | `aqm budget`<br>`[--at <iso>]`<br>`[--prediction PATH]`<br>`[--out PATH]` | **How much may be spent, and by when?** | the state files, prediction | the budget artifact | No | `../03_budgeting/DESIGN.md` |
| 4   | `aqm start_windows`<br>`[--force]` | Is a window open? | Meter only | A minimal message per agent needing one | **Yes**, ≈ $0.02 | `../04_start_windows/DESIGN.md` |
| 5   | `aqm plan`<br>`[--budget PATH]`<br>`[--out PATH]` | **What should run, where, with which AI parameters?** | the state files, budget, config | the plan artifact | No | `../05_planning/DESIGN.md` |
| 6   | `aqm execute`<br>`[--plan PATH]`<br>`[--max-parallel K]`<br>`[--force]` | Run it | plan | Reports, runs, burn-rate samples | **Yes**, up to `amount` | `../06_execution/DESIGN.md` |
| —   | `aqm pipeline`<br>`[--dry-run]`<br>`[--force]`<br>`[--ignore-prediction]` | All of the above, in order | — | — | Via 4 and 6 | §2, §4 |

**Every stage is a pure function of explicit inputs.** A stage consumes the artifact of the stage before it and produces a new one; by default it reads the latest and writes a fresh timestamped file (§5), and the flags select a different one. That is what makes a stage testable on fixtures and runnable against an alternative input — a past artifact, a notebook's experimental predictor, a hand-written budget:

```text
aqm predict --at 2026-09-14T22:00Z --out /tmp/p.json
aqm budget  --at 2026-09-14T22:00Z --prediction /tmp/p.json --out /tmp/b.json
aqm plan    --budget /tmp/b.json --out /tmp/q.json
aqm execute --plan /tmp/q.json --dry-run
```

Ingested history has no flag either: `state/meter-<agent>.jsonl` and `state/buckets.jsonl` are the only place it lives (`../01_ingestion/DESIGN.md` §6), and tests pass a path through the Python API rather than the CLI. There is no `--agent` flag either: every stage covers **all configured agents** and writes one artifact per tick holding a section per agent, so the chain stays one file per step. `execute` takes no agent, amount or deadline either: the plan artifact already carries the mandate for every agent.

**Inputs default to the latest artifact**, resolved through `state/latest/<kind>.json`, which is why `--prediction`, `--budget` and `--plan` are optional: a hand-run stage needs no arguments, and the flags exist to point at a fixture, a past artifact or an experimental predictor. Two rules keep that default honest:

- **Stale inputs are refused.** An input artifact older than `MAX_INPUT_AGE` produces "stale prediction" / "stale budget" / "stale plan" rather than a silent decision on old data — the case that matters is a stage having failed on the previous tick. `--force` overrides it.
- **The supervisor executes a fixed plan.** `aqm execute` reads its plan once and works to it: the mandate cannot move under a running supervisor. The room left for the user was computed at plan time, at most one tick earlier, and is not re-checked during the run (`../06_execution/DESIGN.md` §5).

Stage 6 is a **detached supervisor**, one process covering both agents. Stages 1, 2, 3 and 5 are read-only and safe to run by hand at any time; 4 and 6 carry their own guards (§2), so running them by hand is exactly as safe as letting the scheduler do it, and `aqm pipeline` adds only ordering and the lock.

### 1.2 No other commands, and the backtest knob

`aqm plan` already prints the ranked task queue, so there is no separate `tasks` command. Everything else a human might want — current state, weekly waste, blocking incidents, burn rate, calibration — is read from the logs and the state files in a notebook (§8). Installation is `install.sh` / `uninstall.sh` (§10), and the on/off switch is the `enabled` flag in `config.json` (§6).

Every command prints a short human-readable summary of what it did or decided, and `--json` prints the same content for a script.

**Global options:** `--json`, `--dry-run`, `--verbose`. **Exit codes:** `0` success, `1` error, `2` refused by a guard, `3` locked.

`--ignore-prediction` is refused unless `--dry-run` is given; it exists because running the pipeline by hand is itself organic activity, which leaves no room.

**`--at <iso>` is the backtest knob.** `predict` and `budget` recompute as of a past moment, reading only data that existed then (the state files keep the full history). **`--at` never writes to live state:** the stage prints its output and writes a file only when `--out` is given, so a backtest can never add artifacts to the live tree or move a `latest` symlink. A backtest is a loop over timestamps chaining the read-only stages as shown in §1.1, with its artifacts written under `/tmp` — no separate replay command, and the loop lives in the notebook where its output is analysed.

### 1.3 Files touched

| Direction | Path                                                     | Purpose                                                               |
| --------- | -------------------------------------------------------- | --------------------------------------------------------------------- |
| Reads     | `~/opt/agent-usage-tracker/data/claude/account.jsonl`    | Claude meter                                                          |
| Reads     | `~/opt/agent-usage-tracker/data/codex/account.jsonl`     | Codex meter                                                           |
| Reads     | `~/opt/agent-usage-tracker/data/claude/<session-id>.jsonl` | Per-request and per-session dollars, for attribution only — read only when a bucket overlaps one of our runs (`../01_ingestion/DESIGN.md` §3) |
| Stats     | `~/.claude/projects/*/*.jsonl`, `~/.codex/sessions/**/*.jsonl` | **mtime only, contents never read** — the liveness tripwire (`../01_ingestion/DESIGN.md` §3) |
| Writes    | `~/opt/agent-quota-maximizer/state/`                     | Ingested history, latest symlinks, sessions, runs, locks (§5)         |
| Writes    | `~/opt/agent-quota-maximizer/artifacts/`                 | Timestamped prediction, budget and plan artifacts (§5)                |
| Writes    | `~/opt/agent-quota-maximizer/reports/`                   | Task reports and their index (`../05_planning/DESIGN.md` §5)          |
| Writes    | `~/opt/agent-quota-maximizer/logs/`                      | Pipeline log, executor log, metrics                                   |

**Runtime:** `/usr/bin/python3` (macOS system Python, 3.9), standard library only, so the job never breaks when a Conda environment moves. Configuration is JSON rather than TOML for the same reason (`tomllib` needs 3.11).

## 2. Guards

The guards live in the two stages that act, so running `aqm start_windows` or `aqm execute` by hand is exactly as safe as letting the scheduler do it. `aqm pipeline` adds only ordering and the lock.

```text
aqm pipeline:
  config = load_config()                              # invalid or unreadable → exit "config error"
  if not config.enabled:                              exit "disabled"
  if the pipeline lock is held:                       exit "busy"
  ingest(); predict(); budget()                       # stages 1-3, read-only
  if freshest reading older than READING_MAX_AGE:     exit "stale"    # the meter may have moved
  housekeeping()                                      # log rotation, daily counter, artifact and report pruning
  start_windows()                                     # stage 4, its own guards
  plan()                                              # stage 5: amount, deadline, start_after, queue
  if no agent has amount ≥ MIN_TASK_BUDGET:           exit "nothing due"
  if the executor lock is held:                       exit "already working"
  if every agent's start_after is in the future:      exit "too early"
  spawn detached: aqm execute

aqm execute (reads the latest plan, once):
  per agent with amount ≥ MIN_TASK_BUDGET and now ≥ start_after:
      run its queue until amount, deadline, GUARD_PCT or config says stop
```

The room test that keeps a busy agent out of the plan entirely is part of stage 5 (`../05_planning/DESIGN.md` §3), and in the MVP that is the only time room is computed.

One supervisor covers both agents, since the plan carries both mandates and their quotas are independent; what they share is the repo locks and the global worker cap.

## 3. Safety, failures and recovery

| Risk                                          | Mitigation                                                                                                               |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Runaway spending                              | `amount` per chunk and per agent, `MAX_DAILY_UNITS` per day, `MIN_TASK_BUDGET` at the tail end                           |
| Overshoot multiplied by parallelism           | Reservation before launch (`../06_execution/DESIGN.md` §3)                                                               |
| Two tasks in one repo                         | Repo lock across agents (`../06_execution/DESIGN.md` §2)                                                                 |
| Machine overloaded                            | `MAX_PARALLEL_TOTAL` slot semaphore (§4)                                                                                 |
| Blocking the user                             | Late start, the room test at plan time, `GUARD_PCT` during the run, `MIN_MARGIN` never filled                            |
| Writing to a repo                             | Read-only flags and sandbox (`../06_execution/DESIGN.md` §2); reports written outside repos                              |
| Secrets in reports                            | Excluded repos in config, prompt instruction, scrubber (`../05_planning/DESIGN.md` §5)                                   |
| Bad config                                    | Invalid or unreadable config is a hard stop: nothing runs                                                                |
| Emergency stop                                | Set `enabled: false` in `config.json`; pipeline and supervisor both re-read it, so a run stops at the next task boundary |
| CLI failure (auth expired, rate limit, crash) | Run record stores `is_error` and stderr; two consecutive failures stop the supervisor and set `last_error`               |
| A task hangs                                  | `TASK_TIMEOUT`                                                                                                           |
| Silent death of the job                       | Every tick appends to `logs/metrics.jsonl`; a gap there is the signal, visible in the notebook (§8)                      |
| Machine asleep                                | Ticks are skipped and nothing is assumed; the next tick re-plans from live readings (§4)                                 |
| Corrupted state                               | Every state file written atomically (`.tmp` + `os.replace`); an unparsable file is renamed aside and rebuilt             |

## 4. Services, scheduling and locks

**One launchd job** and **one detached supervisor**, never a service, so a long task never blocks the schedule.

| Job      | Label                                  | Schedule                         |
| -------- | -------------------------------------- | -------------------------------- |
| Pipeline | `com.jeanlescut.agent-quota-maximizer` | `StartInterval 300`, `RunAtLoad` |

The plist follows the conventions of `agent-usage-tracker` (formerly `agent-statusline`) **[verified from its plist]**: `ProcessType Background`, `StandardOutPath`/`StandardErrorPath` into the project's own `logs/`, the real file in `~/opt/agent-quota-maximizer/`, a symlink in `~/Library/LaunchAgents/`.

**Why 5 minutes.** The decision is cheap, the meter has 1% resolution, and a tick arriving 5 minutes late costs at most 5 minutes of a chunk's tail. Shorter would add nothing; longer would blunt the late start of `../05_planning/DESIGN.md` §1.

**What a tick is allowed to cost.** Stages 1–3 run on every one of the 288 ticks a day, whether or not anything is due, so the whole read-only chain is budgeted at **about one second of wall clock, no network calls and no subprocesses** — ingestion dominates it and is itself held under a second (`../01_ingestion/DESIGN.md` §7, `../02_prediction/DESIGN.md` §7, `../03_budgeting/DESIGN.md` §6). On the great majority of ticks `amount_due` is 0 and the tick ends right there, so that second is the entire cost of running this system.

That budget is a design constraint, not an aspiration: a system whose job is to avoid being in the user's way cannot itself be a background process that wakes up every 5 minutes and does real work. The pipeline lock turns any breach into a skipped tick rather than a pile-up (§2), and a tick's duration is recorded in `logs/metrics.jsonl` so a regression is visible in the notebook (§8).

**Sleep.** `StartInterval` does not fire while the Mac sleeps; launchd fires once on wake **[verified in the statusline plist's own comment]**. Every tick re-plans from live readings, so a missed tick costs an opportunity, never correctness.

**Locks.** All `flock`-based, each holding the owner's PID; a lock whose PID is dead is reclaimed.

| Lock                                                     | Guarantees                                              |
| -------------------------------------------------------- | ------------------------------------------------------- |
| `state/locks/pipeline.lock`                              | One pipeline run at a time                              |
| `state/locks/executor.lock`                              | One supervisor at a time, covering both agents          |
| `state/locks/repo-<slug>.lock`                           | One task per repo, across both agents                   |
| `state/locks/slot-<n>.lock`, n = 1..`MAX_PARALLEL_TOTAL` | Counting semaphore capping workers machine-wide         |

## 5. State, artifacts and history

Three kinds of file, with three different lifetimes.

**1. Ingested history — append-only JSONL, disposable and rebuildable (`../01_ingestion/DESIGN.md` §7).**

| File | Content |
|---|---|
| `state/meter-<agent>.jsonl` | Envelope-filtered meter readings, ~30 rows/day/agent — the audit trail (`../01_ingestion/DESIGN.md` §6.1) |
| `state/buckets.jsonl` | The tidy 5-minute table every later stage reads (`../01_ingestion/DESIGN.md` §6.2) |

**2. Artifacts — immutable, one per stage per tick, never overwritten.**

```text
artifacts/predictions/2026-09-23/143607.json     one file per tick, a section per agent
artifacts/budgets/2026-09-23/143607.json
artifacts/plans/2026-09-23/143607.json
state/latest/prediction.json                     symlink → the newest artifact of that kind
```

Each file is named after its `computed_at` and carries `computed_at`, `method` and a `config_hash`, so a past decision can be read back exactly as it was taken — a config edit is the one thing that would otherwise make it unexplainable. Date-sharded directories mean pruning is a directory unlink and no listing grows without bound; the `latest/` symlinks, swapped atomically after each write, make the default input O(1) with no globbing.

Disk: about 9 KB per tick across both agents ≈ **2.5 MB a day**, pruned at `ARTIFACT_RETENTION_DAYS` during housekeeping, so roughly 35 MB in steady state.

**3. Mutable state and logs — current values and history.**

| File | Content |
|---|---|
| `state/sessions.jsonl` | One line per session started: uuid, purpose (`task` / `window-open`), start time, repo, category |
| `state/runs.jsonl` | One line per finished task: session id, repo, category, model, effort, cost, duration, timeout/error flags, scrubbed-secret count |
| `state/burnrate.jsonl` | Samples: units per hour per worker and aggregate, model, effort, workers running at the time |
| `state/pairs.json` | Last run time per (repo, category) |
| `state/daily.json` | Units spent today, rolled over at local midnight |
| `state/last_error.json` | Last failure |
| `state/locks/` | §4 |
| `logs/pipeline.log`, `logs/executor.log`, `logs/metrics.jsonl` | Rotated at `LOG_MAX_MB` |

Every write is atomic (`.tmp` + `os.replace`, then the symlink swap), so a crash or a sleep never leaves a half-written file and a reader never sees a partial one.

**Why immutable artifacts rather than one file per stage overwritten each tick:** a running supervisor reads the plan while the next tick is already writing a new one, and with immutable files it keeps reading exactly the artifact it was launched against unless it deliberately asks for the latest. It also makes "why did it spend 0.6 units at 03:12?" three files to open rather than a replay to trust.

## 6. Configuration (`config.json`)

```json
{
  "enabled": true,
  "disabled_reason": null,
  "agents": ["claude", "codex"],
  "repos": [
    {"path": "~/dev/agent-statusline", "weight": 1.0, "categories": ["bugs", "agent-docs", "tests"]},
    {"path": "~/dev/some-trading-bot", "weight": 1.5}
  ],
  "excluded_repos": ["~/dev/repo-with-secrets"],
  "categories": {"bugs": 1.0, "agent-docs": 0.5, "tests": 0.5},
  "models": {"cheap": "haiku", "default": "sonnet", "expensive": "opus"},
  "parameters": {"MIN_MARGIN": 0.1, "RATE_WINDOW": 30, "QUIET_HOURS": [0, 9]}
}
```

Unknown keys are rejected loudly and `parameters` overrides the defaults of §11. An invalid or unreadable file stops the system (§3), so a typo is an emergency brake rather than a silent misconfiguration, and every command validates the file on startup.

## 7. Time handling

- Everything internal is epoch seconds (UTC).
- `QUIET_HOURS`, the daily cap and report dates use `Europe/Paris` via `zoneinfo`, so summer and winter changes follow the user's day.
- Reset times come from the meter, never from a weekday rule.

## 8. Observability

Everything the system decides is logged; the analysis happens in a notebook against these files, not in the CLI.

| File                   | One line per                              | Fields                                                                                                |
| ---------------------- | ----------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `logs/metrics.jsonl`   | Tick, per agent                           | Timestamp, meter percentages, window state, `amount_due`, predicted p95, room, decision, reason       |
| `state/runs.jsonl`     | Finished task                             | Session id, repo, category, model, effort, cost, duration, timeout/error flags, scrubbed-secret count |
| `state/burnrate.jsonl` | Finished task                             | Units per hour per worker and aggregate, model, effort, workers running at the time                   |
| `state/buckets.jsonl`  | Agent × 5-minute bucket                   | Organic and extra movement, window state, censoring flags (`../01_ingestion/DESIGN.md` §6.2)          |

Together they answer the questions worth asking, with the definitions a notebook needs:

| Question               | How it is computed                                                                                                                                |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Waste                  | Weekly meter percent left at the last reading before each reset, in units                                                                         |
| Extra spend            | Sum of `extra_pct` in `state/buckets.jsonl` over the week                                                                                                   |
| **Blocking incidents** | Organic activity meeting a saturated 5-hour window within `BLOCKING_WINDOW` of extra work running in that window — **the number that must stay at zero** |
| Near misses            | Runs where the user became active while workers were still running — the metric that decides whether the live room re-check is worth building (`../06_execution/DESIGN.md` §5) |
| Forecast calibration   | Share of windows where actual organic demand exceeded the predicted p95; should be ≤ 5%                                                           |
| Burn rate              | Median units per hour, per model, per worker and aggregate                                                                                        |
| Unreachable quota      | `unreachable` from the last tick before each reset                                                                                                |

## 9. Testing

Each stage owns its own tests (see its DESIGN.md). Across stages:

- **Activity tests** on copied quota-log fixtures: our own run intervals overlapping user activity, an idle open session, an unreadable directory, a clock jump.
- **Backtest** (a script or notebook chaining `predict --at` → `budget --at` → `plan` over the recorded month, with a simulated executor at a given burn rate): reports units spent, units wasted, and how often workers would have been running while the user was actually active. This is how parameters get chosen and how this policy is compared with the alternatives of `../03_budgeting/PREVIOUS_IDEAS.md` §7.
- **Dry run end to end** before anything may spend.

## 10. Deployment

`install.sh`, at the top of `release/` in the source repo: copy `aqm/` and the plist into `~/opt/agent-quota-maximizer/`, create `state/`, `logs/`, `reports/`, write `config.json` from the template if none exists (with every `~/dev` repo listed and `enabled: false`), symlink the plist into `~/Library/LaunchAgents/`, symlink `~/.local/bin/aqm`, then `launchctl bootout` + `bootstrap`. Idempotent, and it never overwrites an existing `config.json` or anything in `state/`.

`uninstall.sh` unloads the job and removes both symlinks, leaving state and reports in place.

Turning the system on and off afterwards is the `enabled` flag in `config.json` (§6): the pipeline and any running supervisor both re-read it, so flipping it to `false` stops a run at the next task boundary.

Development happens only in `~/dev/agent-quota-maximizer/release/` (`~/AGENTS.md`); `~/opt/agent-quota-maximizer/` holds the running copy, its state and its logs.

## 11. Parameters

| Parameter                              | Meaning                                                                                                               | Value             |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ----------------- |
| `MIN_MARGIN`                           | Room never filled in any window: the stand-in for a forecast where none exists yet                                    | 0.1 unit          |
| `PERSISTENCE_HOURS`, `PERSISTENCE_P95` | How long the current organic rate is assumed to persist, and the p95 multiplier on it                                 | 2 h, 1.5          |
| `WINDOW_GAP`                           | Assumed gap between consecutive windows                                                                               | 5 min             |
| `DEADLINE_MARGIN`                      | Safety before a chunk's end                                                                                           | 5 min             |
| `RATE_WINDOW`                          | Span over which the organic spend rate is measured                                                                    | 30 min            |
| `TRIPWIRE_RATE`                        | Rate assumed when S2 or S3 fire before spend can be priced                                                            | $0.05/min         |
| `READING_MAX_AGE`                      | Freshness required of a meter reading                                                                                 | 10 min            |
| `MAX_INPUT_AGE`                        | Freshness required of an input artifact, i.e. two ticks (§1.1)                                                        | 10 min            |
| `READ_TAIL_KB`                         | Tail read when a cursor is lost                                                                                       | 256 KB            |
| `ROLLUP_BUCKET`                        | Rollup granularity                                                                                                    | 5 min             |
| `QUIET_HOURS`                          | Hours using the gentle profile                                                                                        | 00:00–09:00 local |
| `GENTLE_RATE`, `BURST_RATE`            | Intended burn rate per profile                                                                                        | 0.2, 1.0 unit/h   |
| `SAFETY`                               | Lead-time multiplier                                                                                                  | 1.5               |
| `ESCALATE_RATE`                        | Required rate above which models and workers escalate                                                                 | 0.5 unit/h        |
| `WINDOW_OPEN_COOLDOWN`                 | Between two opening attempts                                                                                          | 15 min            |
| `MIN_TASK_BUDGET`                      | Below this, do not start another task                                                                                 | 0.05 unit         |
| `MAX_DAILY_UNITS`                      | Cap on extra work per day, per agent                                                                                  | 3 units           |
| `MAX_PARALLEL_PER_AGENT`               | Workers per agent                                                                                                     | 2                 |
| `MAX_PARALLEL_TOTAL`                   | Workers on the machine                                                                                                | 3                 |
| `GUARD_PCT`                            | Window room below which running workers are terminated                                                                | 15%               |
| `GRACE`                                | Time given to workers after the deadline                                                                              | 2 min             |
| `METER_POLL`                           | Meter re-read interval in a supervisor                                                                                | 60 s              |
| `TASK_TIMEOUT`                         | Wall clock per task                                                                                                   | 20 min            |
| `TICK_INTERVAL`                        | Scheduler period                                                                                                      | 5 min             |
| `MIN_BURN_SAMPLES`                     | Samples before trusting the measured rate                                                                             | 10                |
| `BLOCKING_WINDOW`                      | Window for counting a blocking incident                                                                               | 30 min            |
| `ARTIFACT_RETENTION_DAYS`              | Prediction, budget and plan artifacts older than this are pruned                                                      | 14                |
| `REPORT_RETENTION_DAYS`                | Reports older than this are deleted                                                                                   | 90                |
| `LOG_MAX_MB`                           | Rotation threshold                                                                                                    | 20 MB             |
