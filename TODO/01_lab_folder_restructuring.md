# TODO 01 — move all run-by-hand Python into one `lab/` folder

**Status:** ready to execute. **Written:** 2026-10-06. **Author:** a session that did the stage split (`c5bd27a`) and then stopped, because a second agent was committing to this repo in parallel.

**One agent takes this file, does the whole thing, and deletes it.** Do not split it between agents: it is a tree-wide rename, and a half-applied rename leaves cross-references pointing at nothing.

---

## 0. Read this first: the repo has two agents working on it

That is why this file exists instead of the change itself.

| | |
|---|---|
| `c5bd27a` | **this** session — split `release/aqm.py` into the `aqm/` package, 256 assertions |
| `bf35b6b` | **another** session — a design review that parks stage 2, moves the bot to the VM, removes `GUARD_PCT` and `MAX_DAILY_UNITS` |
| `6fa206e` | the same other session — redraws `design/PIPELINE_MAP.html` for that design |

```
  792c386 ─ … ─ 382ee22 ─ c5bd27a ─ bf35b6b ─ 6fa206e ─ (possibly more by now)
                   │          │        └────────┬───────┘
              origin/main    me        the other agent, still working
                   └────────── 3+ commits UNPUSHED ──────────┘
```

HEAD moved twice in the half hour this file took to write, so **do not expect it to equal `6fa206e`** — that is normal here and not a reason to stop. What this task actually depends on is narrower:

```bash
cd ~/dev/agent-quota-maximizer
git status --porcelain                                   # must be empty
ls -d design/02_prediction/lab notebook                  # both must still exist
ls design/02_prediction/*.ipynb | wc -l                  # must be 7
git log --oneline -1 -- release/ test/                   # must still be c5bd27a
bash test/run.sh | tail -1                               # must pass before you start
```

**Stop and ask the user** only if one of those fails: a dirty tree means someone is mid-change, and a commit to `release/` or `test/` after `c5bd27a` means P2b (§11) has begun and will collide with §7. Further commits to `design/` alone are fine — they land in files §6 tells you to sweep anyway.

**Do not push.** The user has not asked for it, the commits ahead of `origin/main` are not this task's to publish, and the GitHub repo is **public**.

---

## 1. Why this change

The user's words: *"I know that there is a lot of Python code in `design/` for the prediction stage. So it's a bit ugly."*

Two concrete problems:

```
  design/     AGENTS.md promises "No code, with one exception: 02_prediction/ holds the bench"
              └── a documented exception is the smell; delete the exception, not reword it

  notebook/   named after a FILE FORMAT, not a purpose
              └── and pipeline_replay.py is not a notebook at all — it is a stdlib script
```

## 2. The name is `lab/`, not `dev/`

The user suggested `dev/`. It was argued down and the user has not objected, but **if they ask, this is the reasoning** — do not re-litigate it silently:

| Against `dev/` | |
|---|---|
| Path collision | the repo is already at `~/dev/agent-quota-maximizer`, so `~/dev/agent-quota-maximizer/dev/` reads as a nested `~/dev` |
| Wrong implication | "dev" suggests `release/` is not development, which is false |
| **`lab` is already the project's word** | it is `design/02_prediction/lab/` today. `~/.claude/CLAUDE.md` is explicit: *do not coin a new term when one exists*, and *one concept, one word* |

## 3. The target layout

Mirror `design/`'s stage numbering, so a `lab/` folder and its `design/` counterpart pair up by eye.

```
lab/                            run by hand · may import pandas, sklearn, anything
├── README.md                   the rule, and how to run each thing
├── 02_prediction/              ← pairs with design/02_prediction/
│   ├── README.md                 (was design/02_prediction/lab/README.md)
│   ├── dataset.py
│   ├── live_replay.py
│   ├── results/*.json            6 files
│   └── 000_do_nothing.ipynb … 05_comparison.ipynb      7 notebooks
├── 03_budgeting/               ← pairs with design/03_budgeting/
│   ├── explain.py
│   └── explain_budget.ipynb
└── 07_pipeline/                ← pairs with design/07_pipeline/
    └── pipeline_replay.py
```

After this, `design/` holds **only** Markdown and `PIPELINE_MAP.html`, and `notebook/` is gone.

### The one rule, stated once in `lab/README.md`

> `release/` never imports `lab/`. `lab/` may import `release/`.

This is the same rule `test/test_imports.sh` already enforces inside the package, pointed outward. Add it as an assertion — see §7.

## 4. Exact moves

Use `git mv` so history follows. The three clusters are independent (verified: `pipeline_replay.py` imports stdlib only; `explain.py` imports pandas/matplotlib only; `dataset.py` ← `live_replay.py` ← the 7 notebooks is the only coupled group).

```bash
cd ~/dev/agent-quota-maximizer
mkdir -p lab/02_prediction lab/03_budgeting lab/07_pipeline

# the forecasting study
git mv design/02_prediction/lab/dataset.py      lab/02_prediction/dataset.py
git mv design/02_prediction/lab/live_replay.py  lab/02_prediction/live_replay.py
git mv design/02_prediction/lab/README.md       lab/02_prediction/README.md
git mv design/02_prediction/lab/results         lab/02_prediction/results
git mv design/02_prediction/000_do_nothing.ipynb         lab/02_prediction/
git mv design/02_prediction/00_constant_reserve.ipynb    lab/02_prediction/
git mv design/02_prediction/01_baseline_recent_rate.ipynb lab/02_prediction/
git mv design/02_prediction/02_state_clock_table.ipynb   lab/02_prediction/
git mv design/02_prediction/03_sklearn_quantile.ipynb    lab/02_prediction/
git mv design/02_prediction/04_hierarchical_bayes.ipynb  lab/02_prediction/
git mv design/02_prediction/05_comparison.ipynb          lab/02_prediction/

# the budget explainer
git mv notebook/explain.py           lab/03_budgeting/explain.py
git mv notebook/explain_budget.ipynb lab/03_budgeting/explain_budget.ipynb

# the whole-pipeline replay
git mv notebook/pipeline_replay.py   lab/07_pipeline/pipeline_replay.py

# notebook/README.md is NOT moved — it is rewritten as lab/README.md (see section 6)
git rm notebook/README.md
rm -rf design/02_prediction/lab notebook        # __pycache__ leftovers
```

**`__pycache__` is already gitignored** but exists on disk in both old folders — remove it, or a stale `forecast_lab.cpython-311.pyc` will sit in an empty directory for ever.

## 5. Path wiring that must change

These are the only places a path is computed, and all of them break. Line numbers are as of `bf35b6b`.

| File | Line | Now | Must become |
|---|---|---|---|
| `lab/02_prediction/dataset.py` | 96 | `REPO = …resolve().parents[3]` | `parents[2]` — the file moves one level shallower |
| `lab/02_prediction/live_replay.py` | 27 | `sys.path.insert(0, str(HERE))` | unchanged — `dataset.py` is now its sibling, so this keeps working |
| `lab/03_budgeting/explain.py` | 25 | `REPO = …resolve().parent.parent` | `parent.parent.parent` — one level deeper than `notebook/` was |
| the 7 notebooks | cell 1 | `sys.path.insert(0, str(pathlib.Path.cwd() / "lab"))` | `sys.path.insert(0, str(pathlib.Path.cwd()))` — `dataset.py` now sits beside the notebooks, no `lab` subfolder |

`pipeline_replay.py` computes no repo path and needs no change.

**Verify each one by running it, not by reading it.** `parents[N]` off by one fails loudly, which is the good case; a notebook silently picking up a different `dataset.py` is the bad one.

```bash
/usr/bin/python3 lab/07_pipeline/pipeline_replay.py | tail -5      # ~10 s, stdlib only
/opt/anaconda3/bin/python lab/02_prediction/live_replay.py | tail -8
/opt/anaconda3/bin/python -c "import sys; sys.path.insert(0,'lab/02_prediction'); import dataset as D; f=D.build('claude'); print(len(f), 'slots')"
```

For the notebooks, execute them headless rather than opening Jupyter:

```bash
cd lab/02_prediction
for nb in 000_do_nothing 00_constant_reserve 01_baseline_recent_rate 02_state_clock_table 03_sklearn_quantile 04_hierarchical_bayes 05_comparison; do
  /opt/anaconda3/bin/jupyter nbconvert --to notebook --execute --inplace $nb.ipynb \
    && echo "ok $nb" || echo "FAIL $nb"
done
```

Note `05_comparison.ipynb` is the only notebook that reads the test set, and re-executing it is legitimate here because nothing is being tuned — but **do not change any model or hyper-parameter while in these files.** If a notebook fails for a reason other than a path, stop and report it; a forecasting result must not be quietly re-derived as a side effect of a folder move.

## 6. Documentation to update

### 6.1 `lab/README.md` — new, replaces `notebook/README.md`

It must carry: the one rule from §3; a table of the three folders and what each answers; that `lab/` may use pandas/sklearn while `release/` is stdlib-only and why (`design/07_pipeline/DESIGN.md` §1); and the run command for each entry point. Fold in the three rows already in `notebook/README.md` (for `explain_budget.ipynb`, `explain.py`, `pipeline_replay.py`) rather than rewriting their descriptions — `bf35b6b` wrote the `pipeline_replay.py` row and it is accurate.

### 6.2 `AGENTS.md`

- The layout table currently has a `design/` row whose text ends *"No code, with one exception: `02_prediction/` holds the forecasting bench … which belong beside the design question they investigate"*. **Delete the exception clause**; `design/` is now code-free.
- Replace the `notebook/` row with a `lab/` row.
- Fix the `notebook/explain_budget.ipynb` link in the "Reviewing what it did" section.

### 6.3 Every remaining reference

These files name `notebook/` or `design/02_prediction/lab` and must be swept. Two are **comments inside shipped code** and matter most, because `release/` must never look wrong:

| File | What it references |
|---|---|
| `release/aqm/core.py` :58, :62 | `../design/02_prediction/lab/live_replay.py` and `…/lab/README.md`, in the `MIN_HUMAN_RESERVE_PCT` justification |
| `design/VOCABULARY.md` | the notebook row, and §307 |
| `design/03_budgeting/DESIGN.md` | the notebook |
| `design/07_pipeline/DESIGN.md` §12.2 | names `design/02_prediction/lab/live_replay.py` and `notebook/` as importers of the flat `aqm` surface |
| `design/02_prediction/DESIGN.md` §8 | the study's own write-up points at the notebooks |
| `design/DESIGN_v2.md` §8 | `bf35b6b` made this the index of the review; check its paths |
| `design/PIPELINE_MAP.html` | **redrawn by `6fa206e`** after this file was written, so check it against whatever the current version says, not against a remembered one |
| `test/README.md` | mentions the notebook |

Find them, do not trust this list to be complete:

```bash
grep -rIn 'notebook/\|02_prediction/lab\|forecast_lab' \
  --include='*.md' --include='*.py' --include='*.sh' --include='*.html' --include='*.ipynb' . \
  | grep -v '^./.git'
```

## 7. Add the outward-facing half of the import rule

`test/test_imports.sh` (written in `c5bd27a`) already enforces the layering **inside** `release/aqm/`. Add one section to the same file asserting the rule from §3:

- no file under `release/` names `lab`;
- every `lab/**/*.py` that imports `aqm` resolves it through `release/`, so the study can never silently test a stale copy.

Follow that file's existing style: the harness API is `ok` / `bad` / `is` / `section` / `finish` (**not** `ok_msg`), and `source "$(dirname "$0")/harness.sh"`.

**Check the new assertion actually bites** before trusting it — plant a deliberate breach, watch it fail, revert. That is how the layering test was validated in `c5bd27a`, and it caught that the first version of the test was grepping its own docstring.

## 8. Acceptance

All of these, in order:

```bash
cd ~/dev/agent-quota-maximizer
bash test/run.sh                 # expect: all 256+ assertions passed, exit 0
grep -rIn 'notebook/\|02_prediction/lab' --include='*.md' --include='*.py' --include='*.html' . | grep -v '^./.git'
                                 # expect: no hits
ls design/02_prediction/         # expect: DESIGN.md, PREVIOUS_IDEAS.md — no .ipynb, no lab/
find design -name '*.py' -o -name '*.ipynb' | grep -v '^$'
                                 # expect: nothing. design/ is code-free
./release/aqm-cli pipeline       # expect: exit 0, a normal tick
```

Plus the four `lab/` entry points from §5 all running.

## 9. The commit

One commit. Suggested subject: `refactor: all run-by-hand code under lab/, design/ is code-free`.

The body should say what moved and why `lab` rather than `dev` (§2), so the next reader does not reopen it. End with:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

**Commit only. Do not push** — see §0.

## 10. Open question for the user — ask, do not decide

This was put to the user and **not answered** before the handoff was requested:

> Does `pipeline_replay.py` get kept, or deleted?

It was written at 20:09 on 2026-10-05, outside any reported turn, and was found untracked. Since then `bf35b6b` **committed it and built a major design decision on it** (`design/DESIGN_v2.md` §8 — parking stage 2, moving the bot to the VM). So in practice the answer is now *keep*, and §4 moves it. **But confirm**, because the user may not know that a second agent adopted it.

## 11. Explicitly out of scope

Do not do any of these while in here, even if they look adjacent:

| | Why |
|---|---|
| **Pushing** | §0 — two commits by two agents are already unpushed, and the repo is public |
| **`release/install.sh`** / deploying | outward-facing; the user has never authorised a deploy. Nothing in `lab/` is deployed anyway |
| **P2b** — making the code match `bf35b6b`'s design | that is a separate, larger task: removing `GUARD_PCT` and `MAX_DAILY_UNITS`, parking stage 2, the 7.6 week capacity. It will touch `release/aqm/` and every test. Keep it out of a rename commit |
| **Re-tuning anything in a notebook** | §5. A folder move must not change a measured result |
| **Fixing `test_real_data.sh`'s flakiness** | known and pre-existing: it failed once on `planned + unreachable = remaining` at 8.407, reproduced on the pre-split monolith, and now reads 8.496. It reads live data while ingestion may be writing. Worth a separate TODO; **not** this one |
