# Agent Quota Maximizer — Design

Status: **scoping only, not implemented.**

---

## 1. Objectives

**Problem**: Sometimes the 7-days token quota of claude or codex reset to 0 without the user had time to fully utilize it. This usage quota that goes unused before a reset has zero value afterward.

**Agents**: This project should runs on both Claude and Codex (optimize quota usage of both)

**Opportunity**: Build a system that spends leftover capacity on genuine software-maintenance work across `~/dev` repos (so-called "tasks")

**Outputs:** At this stage, we suggest the system to only produce reviewable artifacts rather than changing the code

**Schedule Logic:** A planner re-plans every 30 minutes over the rest of the 7-day period and runs extra work where it interferes least with the user (§2).

**Taxonomy:**

- The total amount of work is called "extra work" and could be empty.
- This consists of a clear list of "tasks" ready to executed (what, where, how)

---

## 2. Budgeting and scheduling of extra work

The system decides **when** and **how much** extra work to run with a planner that re-plans every 30 minutes over the rest of the 7-day period. This is one of the candidate policies of `03_budgeting/PREVIOUS_IDEAS.md`, which also documents the simpler rule-based ones (a fixed nightly top-up needing no prediction, and the waste-free floor alone) and how to choose between them. Its behaviour (running at night, keeping a daily reserve for the user, using the last hours before the reset) is not hard-coded: it follows from a profile of the user's organic usage learned from past data. The reasoning behind each choice is in `CONSIDERATIONS.md` (§1–§8).

### 2.1 Units and inputs

- **Unit:** `1 unit` = the quota of one full 5-hour window. Every amount (remaining quota, reserve, spend) is expressed in units, and converted to and from % and USD with the figures of `~/dev/agent-statusline/adhoc_quotas_analysis/CONCLUSIONS.md` (Claude Pro: 1 unit ≈ $32, a week ≈ 8.85 units; Codex Plus: 1 unit ≈ $20.5, a week ≈ 6.2 units).
- **Quota readings:** read from `~/opt/agent-statusline/state/quota/{claude,codex}` (current) and `~/opt/agent-statusline/data/*-quota-history.jsonl` (history). This system never fetches quotas itself.
- **Window rules:** taken from `CONCLUSIONS.md` §1.1 (activity-triggered 5-hour windows, Claude week fixed at Monday 19:00 UTC, Codex week pinned by the first message and subject to early resets, a weekly reset does not reset the 5-hour window).
- **Organic usage history:** per-message spend from local session files (Claude transcripts, Codex session files). Sessions started by this system are tagged so they can be excluded.
- **Each agent is planned independently**, with its own quota, reset time, profile and parameters.

### 2.2 Components

The usage profile and nowcast below form the `organic_usage_predictor`; its candidate designs are detailed in `02_prediction/PREVIOUS_IDEAS.md`.

| Component | Role |
|---|---|
| **Usage profile** | For each hour of the week (local time, Europe/Paris), the distribution of organic spend and the probability that the user is active. Learned from organic sessions only. To cope with few weeks of data, hours are pooled (weekday vs weekend × hour of day). |
| **Nowcast** | Short-term correction of the profile from recent activity: whether an organic message arrived in the last 30–60 minutes. An active user is likely to stay active over the next hours, an idle one to stay idle. |
| **Planner** | Places the extra spend needed before the reset into the future slots where it interferes least with the user, subject to the quota rules. |
| **Window guard** | Before anything runs, checks that the current 5-hour window keeps enough room for the user's forecast spend in that window. |

### 2.3 Planner loop

Every 30 minutes, for each agent:

```text
R        = remaining 7-day quota (units)
slots    = 30-min slots from now until the 7-day reset
organic  = forecast organic spend per slot (profile, corrected by the nowcast for the next hours)

reserve  = q-quantile of total organic spend from now until the reset
extra    = max(0, R − reserve)                  # quota that would otherwise be wasted

cost(s)  = P(user active in slot s) × blocking_penalty
         + carry_over_penalty   if the 5-hour window of slot s runs past the 7-day reset

fill `extra` into slots by increasing cost(s), subject to:
    - a 5-hour window holds at most 1 unit, minus its forecast organic spend
    - at most the achievable burn rate per slot
    - a window only exists once work starts in it (the plan decides when to open windows)

window guard: in the current 5-hour window, keep room for the p95 of the user's forecast spend

run now only what the plan assigns to the current slot; re-plan at the next tick
```

Only the current slot of the plan is ever executed. Readings, the nowcast and the user's actual usage are re-read at every tick, so errors (a heavy day, a task that cost more than expected, an early Codex reset) are corrected at the next tick.

### 2.4 Resulting behaviour

| Behaviour | Why it follows from the planner |
|---|---|
| Extra work runs at night | Night slots have P(user active) ≈ 0 in the data, so they are the cheapest and are filled first. |
| About 2 units stay free for each remaining day | `reserve` is the p90 of organic spend until the reset. The observed daily p90 is ≈ 2 units (p95 2.11, max 2.21), so roughly 2 units per remaining day are kept. |
| The user's day is compensated at night | A heavy day lowers `R`, so the next plan puts less work into the following night; a light day raises it. |
| The final day is used, not wasted | Near the reset the horizon is short, so `reserve` shrinks to what the user is likely to spend in those few hours. If the nowcast shows the user idle, those slots become cheap and the remainder is spent. |
| No saturated 5-hour window at the start of a new week | The carry-over penalty discourages saturating a window that straddles the 7-day reset while the user may be active. |
| Works for any reset time | Nothing assumes Monday: a Codex reset at 08:00 makes the preceding night cheap, and it gets filled. |
| Windows are opened on purpose | The planner decides when work starts, so it opens a new 5-hour window as soon as the previous one expires when it needs the capacity (see `CONSIDERATIONS.md` §8). |

**Example (Claude, Monday, reset at 21:00 Paris):**

| Time | State | Decision |
|---|---|---|
| 10:00 | R = 1.5, user active this morning | reserve ≈ 1.5, extra ≈ 0: nothing |
| 16:00 | R = 1.0, last message 10 min ago | coming slots are costly (user active): nothing |
| 19:00 | R = 0.9, idle for 75 min, evening profile low | reserve ≈ 0.2, extra ≈ 0.7: spent by 21:00 in the cheapest slots |
| 20:40 | the user comes back | the window guard keeps room for their forecast spend |

### 2.5 Parameters

| Parameter | Meaning | Starting value |
|---|---|---|
| `q` | Quantile of organic spend kept in reserve: higher protects the user more, lower wastes less. May decrease near the reset, where 7-day blocking is mild. | 0.9 |
| `blocking_penalty` | Cost of interfering with an active user | TBD |
| `carry_over_penalty` | Cost of a saturated window that runs past the 7-day reset | TBD |
| Guard quantile | Share of the user's forecast spend protected in the current 5-hour window | 0.95 |
| Slot length and re-plan interval | Planning resolution | 30 min |
| Burn rate | Maximum spend per slot; learned from the system's own runs | TBD (guess until measured) |

### 2.6 Cold start

Until the profile has enough data, it is seeded with simple priors: user inactive between 00:00 and 10:00 local time, about 2 units of organic spend per day. The learned profile progressively replaces the priors.

### 2.7 Open points

- Hour-level profiles rest on a few weeks of data (about 4 examples per hour of the week), hence the pooling.
- The heaviest observed days (≈ 2.1–2.2 units) may have been capped by the quota itself, so the true demand on such days is unknown.
- The achievable burn rate per agent is unknown until the system runs.
- Codex usage from other clients (cloud, IDE, ChatGPT app) is invisible locally, so its profile underestimates organic usage.

---

## 3. Choice of Tasks

Once the planner (§2) assigns a positive budget to the current slot, tasks are selected to fill it. The considerations behind every point below are in `CONSIDERATIONS.md` §9–§12.

### 3.1 Definition of a task

A task has 3 components:

1. **Repo:** the project in `~/dev` to work on.
2. **Category:** what to do (find bugs, review documentation, maintain agent instruction files, ...).
3. **AI parameters:** agent (Claude or Codex), model, effort.

### 3.2 Selection criteria

Tasks are ranked on three dimensions:

- **Value:** importance of the repo, importance of the category, and staleness of the (repo, category) pair.
- **Risk:** consequence if wrong, which sets the AI parameters required for a (repo, category).
- **Cost:** predicted spend (units) and predicted wall-clock duration.

### 3.3 Decisions

- **Repo weights:** seeded in a configuration from simple signals (live deployment, financial stakes, archive-like naming), and overridable by the user.
- **Bugs are the highest-priority category** (explicit user decision).
- **Staleness** of a (repo, category) pair is a function of the time since its last run and of the relevant change in the repo since then, excluding changes prompted by the system's own reports.

### 3.4 Open questions

- In-scope repo list and per-repo weights (not yet given by the user).
- Full category list and its ordering beyond "bugs first".
- How applicability of a category to a repo is decided (LLM, human, configuration).
- How value, risk and cost are merged into one ranking.
- How spend and duration are predicted (probably from past similar tasks).
- Whether to use only the most capable AI parameters (for example Opus at high effort), since the quota would otherwise be wasted.
- Whether to schedule the largest, most uncertain tasks early in the week and smaller ones near the reset.

---

## 4. Execution of Tasks

The considerations behind this section are in `CONSIDERATIONS.md` §13–§17.

### 4.1 Decisions

- **Sequential execution for the MVP.** Parallelism may come later, and then only across independent repos (separate git trees, no shared write target).
- **Read-only, report-only.** Tasks produce reviewable artifacts and never change the code.
- **Outputs are easy to review for a human:** minimal, well structured, graphical.

### 4.2 Open questions

- Whether a task that would run past the 7-day reset should be avoided or stopped.
- Wall-clock timeout per task, and if so a graceful stop with a best-effort checkpoint rather than a kill.
- Coordination with `auto-commit`, so that no repo gets two unattended writers.
