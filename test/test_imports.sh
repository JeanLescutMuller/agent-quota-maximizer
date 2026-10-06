#!/bin/bash
# The stage boundaries, enforced on the code and not only on the artifacts.
#
# `test_predict.sh` already proves that budget never READS `internals`. This proves the
# stronger thing: budget *cannot*, because it does not import the module `internals`
# lives in. A stage's only interface with its neighbours is the artifact, and an import
# is how that rule would quietly stop being true.
#
# It also pins the layering. Splitting the monolith produced two cycles, both from one
# generic helper filed under a stage (`read_all` under ingestion, `clock` under the
# pipeline); this is what catches the third one.
# ../design/07_pipeline/DESIGN.md 1
set -uo pipefail
source "$(dirname "$0")/harness.sh"

PKG="$REPO/release/aqm"

# What each module may import from inside the package, and nothing else. Read down the
# list: each line may only use the ones above it.
allowed() {
    case "$1" in
        core)       echo "" ;;
        io)         echo "core" ;;
        history)    echo "core io" ;;
        s1_ingest)  echo "core io" ;;
        s2_predict) echo "core io history" ;;
        s3_budget)  echo "core io history plumbing" ;;
        plumbing)   echo "core io" ;;
        pipeline)   echo "core io history plumbing s1_ingest s2_predict s3_budget" ;;
        cli)        echo "core io history plumbing s1_ingest s2_predict s3_budget pipeline" ;;
        *)          echo "?" ;;
    esac
}

section "every module is in the layering table"
for f in "$PKG"/*.py; do
    mod="$(basename "$f" .py)"
    case "$mod" in __init__ | __main__) continue ;; esac
    if [ "$(allowed "$mod")" = "?" ]; then
        bad "release/aqm/$mod.py is not in the layering table -- add it on purpose"
    else
        ok "$mod is in the table"
    fi
done

section "no module imports outside its allowance"
for f in "$PKG"/*.py; do
    mod="$(basename "$f" .py)"
    case "$mod" in __init__ | __main__) continue ;; esac
    permitted=" $(allowed "$mod") "
    [ "$permitted" = " ? " ] && continue
    for dep in $(grep -oE '^from \.[a-z_0-9]+ import' "$f" \
                 | sed 's/^from \.//; s/ import$//' | sort -u); do
        case "$permitted" in
            *" $dep "*) ok "$mod may import $dep" ;;
            *)          bad "$mod imports $dep, which the layering forbids" ;;
        esac
    done
done

section "budget cannot see the prediction engine, only its artifact"
# Parsed, not grepped. The first version of this test grepped the file text and failed
# on its own docstring, which *explains* the rule -- prose naming a forbidden thing is
# documentation, and only code can break a contract.
CODE="$("$PY" - "$PKG/s3_budget.py" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
# A docstring that explains the rule is documentation, not a breach of it; a *key* or
# an attribute is how the rule would actually be broken.
docs = {id(ast.get_docstring(n, clean=False) and n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
        and ast.get_docstring(n)}
names, strings = set(), 0
for n in ast.walk(tree):
    if isinstance(n, ast.Name):
        names.add(n.id)
    elif isinstance(n, ast.Attribute):
        names.add(n.attr)
    elif isinstance(n, ast.alias):
        names.add(n.name.lstrip("."))
    elif (isinstance(n, ast.Constant) and isinstance(n.value, str)
          and id(n) not in docs):
        strings += ("internals" in n.value) or ("s2_predict" in n.value)
print("predict_module" if "s2_predict" in names else "-",
      "internals_name" if "internals" in names else "-",
      "internals_key" if strings else "-")
PY
)"
is "no code in s3_budget.py refers to s2_predict or internals" "$CODE" "- - -"

section "core stays a leaf, so no cycle can form"
is "core.py imports nothing from the package" \
   "$(grep -cE '^from \.[a-z]' "$PKG/core.py")" "0"

section "the package keeps the flat surface test/ and lab/ import"
FLAT="$("$PY" -c "
import sys; sys.path.insert(0, '$REPO/release')
import aqm
need = ('P agents budget clock config_hash home ingest load_config meter_now meter_path'
        ' METER_TYPES predict predict_agent read_csv recent_slots shown slots_path'
        ' SLOT_SECONDS SLOT_TYPES usage_dir when FIVE_HOURS').split()
missing = [n for n in need if not hasattr(aqm, n)]
print(' '.join(missing) if missing else 'flat')
" 2>&1)"
is "import aqm still exposes every name they use" "$FLAT" "flat"

# live_replay.py and the notebooks patch aqm.P and expect the stage to see it. That
# works only because P is mutated and never rebound; `from .core import P` binds the
# same dict. If anyone ever writes `P = {...}` in a module, this is what catches it.
SHARED="$("$PY" -c "
import sys; sys.path.insert(0, '$REPO/release')
import aqm
aqm.P['SAFETY_MULTIPLIER'] = 9.9
from aqm import s2_predict, s3_budget, history, plumbing
print(all(m.P['SAFETY_MULTIPLIER'] == 9.9
          for m in (s2_predict, s3_budget, history, plumbing)))
" 2>&1)"
is "patching aqm.P reaches every stage module (one dict, mutated)" "$SHARED" "True"

section "every entry point still works"
is "release/aqm-cli --help" \
   "$("$PY" "$REPO/release/aqm-cli" --help >/dev/null 2>&1 && echo ok)" "ok"
is "release/aqm --help  (the package directory, via __main__.py)" \
   "$("$PY" "$REPO/release/aqm" --help >/dev/null 2>&1 && echo ok)" "ok"
is "python3 -m aqm --help" \
   "$(cd "$REPO/release" && "$PY" -m aqm --help >/dev/null 2>&1 && echo ok)" "ok"

# ../design/07_pipeline/DESIGN.md 9: in full, not through env, so the symlink on PATH
# cannot inherit whichever python3 a Conda environment happens to put first.
is "aqm-cli names the interpreter in full" \
   "$(head -1 "$REPO/release/aqm-cli")" "#!/usr/bin/python3"

section "the monolith is gone, so there is one copy of every stage"
is "release/aqm.py no longer exists" \
   "$([ -e "$REPO/release/aqm.py" ] && echo present || echo absent)" "absent"

section "release/ never imports lab/; lab/ gets aqm only from release/"
# ../lab/README.md: the layering above, pointed outward. Parsed for the same reason as
# the budget test: a comment citing `../lab/...` as evidence is documentation, while an
# import or a path string is how release/ would come to depend on run-by-hand code.
OUT="$("$PY" - "$REPO" <<'PY'
import ast, pathlib, re, sys
repo = pathlib.Path(sys.argv[1]).resolve()

def code_strings(tree):
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
            and ast.get_docstring(n)}
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]

def imported(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            yield from (a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            yield n.module.split(".")[0]

for f in sorted((repo / "release").rglob("*.py")) + [repo / "release" / "aqm-cli"]:
    tree = ast.parse(f.read_text())
    name = f.relative_to(repo)
    if "lab" in imported(tree):
        print("bad %s imports lab" % name)
    if any(re.search(r"(^|/)lab(/|$)", s) for s in code_strings(tree)):
        print("bad %s builds a path into lab/" % name)

def repo_of(f):
    """Evaluate the file's own `REPO = ...` line, so an off-by-one parents[N] shows."""
    for n in ast.walk(ast.parse(f.read_text())):
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "REPO" for t in n.targets):
            return eval(compile(ast.Expression(n.value), str(f), "eval"),
                        {"pathlib": pathlib, "__file__": str(f)})

checked = 0
for f in sorted((repo / "lab").rglob("*.py")):
    tree = ast.parse(f.read_text())
    mods = list(imported(tree))
    if "aqm" not in mods:
        continue
    checked += 1
    # the file puts release/ on the path itself, or a sibling it imports does
    supplier = next((g for g in [f] + [f.with_name(m + ".py") for m in mods]
                     if g.exists() and 'REPO / "release"' in g.read_text()), None)
    if supplier is None:
        print("bad %s imports aqm without putting release/ on the path" % f.relative_to(repo))
    elif repo_of(supplier) != repo:
        print("bad %s: REPO in %s resolves to %s, not the repo root"
              % (f.relative_to(repo), supplier.name, repo_of(supplier)))
    else:
        print("ok %s gets aqm from release/" % f.relative_to(repo))
print("checked %d" % checked)
PY
)"
while read -r verdict msg; do
    case "$verdict" in
        ok)      ok "$msg" ;;
        bad)     bad "$msg" ;;
        checked) [ "$msg" -gt 0 ] && ok "$msg lab/ files import aqm, all checked" \
                                  || bad "no lab/ file imports aqm -- the check found nothing to check" ;;
        *)       bad "unexpected output: $verdict $msg" ;;
    esac
done <<< "$OUT"
is "release/ never imports or points into lab/" \
   "$(grep -c '^bad release/' <<< "$OUT")" "0"

finish
