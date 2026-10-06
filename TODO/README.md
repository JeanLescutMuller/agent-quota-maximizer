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
