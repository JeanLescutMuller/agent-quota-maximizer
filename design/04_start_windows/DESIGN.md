# 04 — Window starter (`aqm start_windows`)

Stage 4, and the first stage that acts. A window exists only because a message opened it, and a gap near the reset removes a whole window from the chain. The policy is the simplest one: **keep a window open at all times.**

| | |
|---|---|
| Command | `aqm start_windows [--force]` |
| Reads | The meter only (`../01_ingestion/DESIGN.md` §2) |
| Writes | A minimal message per agent needing one; a spend record |
| Acts | **Yes**, ≈ $0.02 per opening |
| Consumed by | Nothing — it makes the budget stage's chain of remaining windows true rather than hypothetical |

The context, the measured idle gaps and the worked example of what a missing window costs are in `CONSIDERATIONS.md` next to this file.

## 1. The rule

```text
per agent:
  if no window is open and last attempt older than WINDOW_OPEN_COOLDOWN:
      send one minimal message                  # opens the 5-hour window
  Codex only: if no 7-day period is running (fake countdown, ../01_ingestion/DESIGN.md §5):
      the same message starts the week          # ../CLAUDE_AND_CODEX.md §6.1
```

No chain arithmetic, no plan, no forecast: the only input is "is a window open", one value from the meter. That is why this stage reads nothing else and runs before planning.

## 2. Why always, rather than only when needed

An earlier version computed how many windows were still needed before the reset; that was a premature optimisation, and the numbers say so:

|                                    |                                                                                                                               |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Cost of one opening **[verified]** | $0.0202, ~0.06% of a window, nearly all cache creation                                                                        |
| Maximum frequency                  | One per 5 hours per agent — a window cannot be opened while one is running                                                    |
| Cost of the policy                 | ≈ $0.10 per day per agent, **0.24% of a Claude week**                                                                         |
| Cost of getting it wrong           | A missing window near the reset destroys up to a full unit ≈ $32 ≈ 11% of the week (`CONSIDERATIONS.md` §2)                   |

An open but unused window costs nothing: the 5-hour cap limits speed only, it never consumes weekly quota. The asymmetry is about 45 to 1 in favour of always opening.

On Codex the stakes are higher still: the 7-day period itself starts at the first message after idle, so a period that begins two days late is two days of allowance that never existed (`../CLAUDE_AND_CODEX.md` §6.1).

## 3. Two things the rule deliberately does not check

- **Whether the user is active.** If they are, they open the window themselves and our message is a redundant $0.02.
- **Whether the reset is close.** A window opened just before the reset straddles it, which is desirable: it captures the trailing capacity. The budget stage's window split already stops the post-reset part from being saturated (`../03_budgeting/DESIGN.md` §2).

## 4. The message

`claude -p "ok"` (or `codex exec`) with the cheapest model, a pre-generated session id, and all tools disallowed. It is recorded like any other spend, tagged **extra** so it is never learned as user activity (`../01_ingestion/DESIGN.md` §6).

Within two ticks the system verifies that the meter now shows an open window; if not, it logs a failed opening and retries after the cooldown. A pending resume queued by `agent-auto-resume` also opens windows, and the cooldown keeps the two from racing.

## 5. Acceptance (P3)

A window opens within one tick of the previous one expiring; a Codex period is started after a reset; cost stays around $0.10 per day per agent.

## 6. Open points

- Whether `codex exec` opens a window exactly like an interactive message, and how fast the meter reflects it (`../CLAUDE_AND_CODEX.md` §7).
- Whether a started Codex period can be left unused with no penalty — expected, but unverified over a full week.
- Whether to read `agent-auto-resume`'s pending-resume queue directly, rather than relying on the cooldown alone.
- How precisely the expiry timestamp can be trusted (clock drift, stale readings, integer percentages), which sets the real value of `WINDOW_OPEN_COOLDOWN`.
