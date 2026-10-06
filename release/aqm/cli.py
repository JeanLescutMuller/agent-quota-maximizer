"""Argument parsing and dispatch.

Kept apart from `pipeline` so that `pipeline()` stays callable from a notebook without
argparse in the way.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .core import load_config
from .io import write_artifact
from .pipeline import budget_table, pipeline, when
from .s1_ingest import ingest
from .s2_predict import predict, predict_table
from .s3_budget import budget


#   0 success · 1 error · 2 refused by a guard · 3 locked
#   (../design/07_pipeline/DESIGN.md 1.2)
REFUSED = {"disabled": 2, "stale": 2}
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="aqm")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("ingest", help="read the meter logs into the 5-minute table")
    one.add_argument("--backfill", action="store_true",
                     help="rebuild from the whole history (a few seconds)")
    one.add_argument("--now", type=int, default=None,
                     help="treat this epoch as the current time (tests, replay)")
    one.add_argument("--json", action="store_true")

    two = sub.add_parser("predict", help="how much the user will still want")
    three = sub.add_parser("budget", help="how much may be spent, and by when")
    for p in (two, three):
        p.add_argument("--at", default=None,
                       help="decide as of this moment (epoch or ISO); never writes")
        p.add_argument("--out", default=None, help="write the artifact here instead")
        p.add_argument("--json", action="store_true")
    three.add_argument("--prediction", default=None,
                       help="a prediction artifact (default: state/latest)")

    run = sub.add_parser("pipeline", help="every stage in order: the scheduled job")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--now", type=int, default=None)
    run.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    try:
        load_config()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.command == "ingest":
        report = ingest(backfill=args.backfill, now=args.now)
        elapsed = (time.perf_counter() - started) * 1000
        if args.json:
            print(json.dumps({"stage": "ingest", "ms": round(elapsed, 2),
                              "agents": report}))
        else:
            for agent, r in report.items():
                print("%-7s %4d new readings  %5d new slots  %s windows in the tail"
                      % (agent, r["readings"], r["slots"], r.get("recent_windows", "?")))
            print("%.1f ms" % elapsed)
        return 0

    if args.command == "pipeline":
        report = pipeline(dry_run=args.dry_run, now=args.now)
        if args.json:
            print(json.dumps(report, sort_keys=True))
        else:
            print("%s · %s" % (" → ".join(report["stages"]) or "none",
                               report["decision"]))
            if report.get("budget"):
                print(budget_table(report["budget"]))
        return REFUSED.get(report["decision"], 3 if report["decision"] == "busy" else 0)

    # `predict` and `budget`: `--at` is the backtest knob and must not touch the
    # live tree, so it writes only where it is told to (07_pipeline/DESIGN.md 1.2).
    at = when(args.at)
    if args.command == "predict":
        report, kind, table = predict(at=at), "predictions", predict_table
    else:
        report, kind, table = (budget(at=at, prediction=args.prediction),
                               "budgets", budget_table)
    elapsed = (time.perf_counter() - started) * 1000
    if args.out is not None or at is None:
        write_artifact(kind, report, report["computed_ts"], out=args.out)
    if args.json:
        print(json.dumps(dict(report, ms=round(elapsed, 2)), sort_keys=True))
    else:
        print(table(report))
        print("%.1f ms" % elapsed)
    return 0
