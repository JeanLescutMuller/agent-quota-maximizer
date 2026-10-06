# TODO/ — handoffs between agents

One file per task, each self-contained enough that an agent with no memory of the conversation that wrote it can execute it alone.

This folder exists because more than one agent works on this repository at the same time. A task described only in a conversation is invisible to every other session; a task described here is not.

## The rules

| | |
|---|---|
| **One agent per file** | A handoff is not split between agents. Most of these are tree-wide renames or multi-file edits, and a half-applied one leaves cross-references pointing at nothing |
| **Check the repo state first** | Every file opens with the commit it was written against. If HEAD has moved, **stop and ask** — do not rebase, stash, or guess |
| **Delete the file when done** | In the same commit as the work. A stale handoff is worse than none, because it reads as pending |
| **Open questions stay open** | If a file marks something as awaiting the user, ask them. Do not decide it to unblock yourself |

## Naming

`NN_short_description.md`, numbered in the order they were written. The number is not a priority and not a dependency — if two files must happen in order, the later one says so.

## Open

| File | Task | Blocked on |
|---|---|---|
| [`01_lab_folder_restructuring.md`](01_lab_folder_restructuring.md) | Move every run-by-hand Python file into one `lab/` folder, so `design/` is code-free and `notebook/` is gone | nothing — ready to execute. Carries one open question for the user (§10) |
| [`02_p2b_code_catches_up.md`](02_p2b_code_catches_up.md) | **P2b.** Make `release/aqm/` match the 2026-10-06 design: the week meter never decreases, week capacity measured rather than assumed, the window outliving the weekly reset gets only its share, and stage 2 leaves the tick | **two questions for the user, §5.** Also names two gaps in the design itself (§4) — the capacity formula cannot be computed as written |

The two are independent and either order works. 01 renames folders and touches no logic; 02 touches `release/aqm/` and five test files and no folder names. If 01 runs first, read `notebook/…` in 02 as `lab/…`.

Known and deliberately not yet written up: `test_real_data.sh` is flaky (it asserts `planned + unreachable = remaining` against live data while ingestion may be writing — seen at 8.407 and 8.496 within a minute), and the bot still has to move to the Debian VM (`design/DESIGN_v2.md` §8.1), which is deployment rather than code.
