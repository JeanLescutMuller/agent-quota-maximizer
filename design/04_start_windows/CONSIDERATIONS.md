# Automatic quota window starter — Context and considerations

Considerations for a small service that **opens quota windows on purpose**, instead of waiting for a message to open one by accident. It covers Claude's 5-hour windows and both of Codex's windows (5-hour and 7-day). No design choices here; the general project considerations are in `../CONSIDERATIONS.md` (this service is its §8).

## 1. Context

A quota window is not a calendar slot: it **starts at the first message sent after the previous window expired**, and it lasts 5 hours (or 7 days for Codex's period) from that message. Nothing happens on its own. If no message is sent, no window exists, and the quota it would have carried is simply never available.

Two consequences:

- **The number of windows that fit before the 7-day reset depends on when each window is opened.** Opening them late reduces how much of the weekly quota can still be spent (`../CONSIDERATIONS.md` §6.2, §6.3).
- **For Codex, the 7-day period itself starts at the first message after an idle stretch.** A day of silence after a reset is a day of the next period that never existed, which lowers the total quota received per year (`../CONSIDERATIONS.md` §8).

Measured behaviour (`~/dev/agent-usage-tracker/adhoc_quotas_analysis/CONCLUSIONS.md`): idle gaps between 5-hour windows are the norm, not the exception — 26 of 31 consecutive pairs for Claude (median gap 11.5h) and 21 of 28 for Codex (median 10.0h). Windows cover only 24% of elapsed time for Claude. In normal use this costs nothing, because the user is not trying to spend the whole week. It costs a lot in the last hours before the reset, which is exactly when this project needs the windows.

## 2. Worked example: a 3-hour gap costs 0.6 unit

Claude, reset Monday 21:00 Paris. It is Sunday 22:00 and 4.6 units of the weekly quota are still unspent, so there are 23 hours left to spend them. 1 unit = one full 5-hour window ≈ $32.

**Windows opened as soon as the previous one expires:**

| Window | Usable before the reset |
|---|---|
| 22:00 → 03:00 | 1 unit |
| 03:00 → 08:00 | 1 unit |
| 08:00 → 13:00 | 1 unit |
| 13:00 → 18:00 | 1 unit |
| 18:00 → 23:00 | only 18:00–21:00 counts for this week: about 0.6 unit |
| **Total** | **≈ 4.6 units, nothing wasted** |

**The same night, but nothing is sent until 01:00 (a 3-hour gap):**

| Window | Usable before the reset |
|---|---|
| 01:00 → 06:00 | 1 unit |
| 06:00 → 11:00 | 1 unit |
| 11:00 → 16:00 | 1 unit |
| 16:00 → 21:00 | 1 unit, ending exactly at the reset |
| **Total** | **4 units; 0.6 unit ≈ $19 expires unused** |

The gap did not waste 3 hours of capacity, it removed a whole window's worth of opportunity from the end of the chain. The same arithmetic applies to every gap in the last ~44 hours of a Claude week (~31 hours for Codex), which is the only part of the week where waste can still be created or avoided.

## 3. Considerations

### 3.1 Why a separate service

- **It is useful on its own.** Even with no extra work at all, opening windows at the right time lets the user's own interactive work reach more of the weekly quota.
- **It is cheap and bounded.** Opening a window costs one minimal message (a few cents at most), whatever happens next.
- **It is the precondition of everything else.** The scheduling policies (`../03_budgeting/PREVIOUS_IDEAS.md`) assume a window exists when they decide to spend; the endgame rules there explicitly depend on a window having been opened before a given hour.

### 3.2 When opening a window is worth it

- **Only the last stretch of the period matters.** Earlier in the week there are far more windows available than quota to spend, so opening one early changes nothing. Near the reset, each missing window is capacity that disappears.
- **A window opened too late is truncated by the reset.** The part of a window that runs after the reset is charged to the new week (`../CONSIDERATIONS.md` §5, §13), so it does not rescue anything.
- **An opened window that goes unused costs nothing**, apart from the message itself: the 5-hour cap only limits speed, it does not consume the weekly quota (§6.1).
- **A safety margin is needed on both sides.** Firing exactly at the expiry timestamp risks landing before it (the message then counts in the old window and opens nothing) or too late (a gap). The margin has to cover reading staleness and the integer resolution of the percentages (§5).

### 3.3 Interference with the user

- **A window opened just before the reset carries into the new week.** If extra work then saturates it, the user can be blocked for up to 5 hours at the very start of the fresh period (`../CONSIDERATIONS.md` §13).
- **Opening a window is not spending.** The risk comes from what the scheduling policy does inside it, not from the starter.
- **Someone may already have opened it.** The user's own session, or a session resumed by `agent-auto-resume` (§19), may open the window first. The starter must check before acting and never open two windows in a row.

### 3.4 Codex specifics

- **The 7-day period starts on the first message after idle**, so keeping periods back to back maximises how many of them a year contains, and therefore the total quota received.
- **Early server resets** were observed (08-27, 08-31, 09-12) and can re-anchor both meters, so any schedule computed from a previous reset time can become wrong without warning.
- **The API reports a fake countdown when idle** (0%, reset = now + 5h or + 7d), so "there is a window running" cannot be read naively from the meter.
- **Part of Codex usage is invisible locally** (cloud, IDE, ChatGPT app), so a window may have been opened by a client this machine cannot see.

### 3.5 Practical risks

- **The machine may be asleep** at the moment a window should be opened (`../CONSIDERATIONS.md` §16), so a missed firing must be detected and recovered rather than assumed impossible.
- **The opening message is usage too.** It appears in the history and must be tagged, or it will be learned as human user activity (§7.8).
- **Unknown:** whether a trivial message (a one-token prompt) reliably opens a window on each agent, and what the cheapest such message is.
- **Unknown:** how precisely the expiry timestamp can be trusted (clock drift, stale readings, integer percentages).
- **Unknown:** Codex's unattended launch path (`../CONSIDERATIONS.md` §15).

## 4. What the service must decide

| Question | Why it is open |
|---|---|
| When to open a window | Only useful near the reset, but "near" depends on the remaining quota and the achievable burn rate (`../CONSIDERATIONS.md` §6.6) |
| How large a safety margin after expiry | Trade-off between a gap and a message that lands in the old window |
| Whether to open a window that will straddle the reset | It captures trailing capacity but carries a saturated window into the new week |
| How to coordinate with `agent-auto-resume` and with the user's own sessions | Avoid opening a window that is about to be opened anyway |
| How to recover a missed firing | The machine may have been asleep |

The design built on these considerations is in `DESIGN.md` next to this file.
