#!/usr/bin/env python3
"""Attempt one strict zero-delay replay of each complete source in a collection."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from run_pilot import HERE, RUNS, save
from audit_replay import audit
from replay_paths import replay_run_name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    if not args.name.replace("-", "").replace("_", "").isalnum():
        parser.error("Invalid name")
    collection = args.collection.resolve()
    plan = json.loads((collection / "plan.json").read_text())
    if len(json.loads((collection / "results.json").read_text())) != len(plan["tasks"]):
        parser.error("Collection is still running or incomplete")
    out = RUNS / args.name
    out.mkdir(parents=True, exist_ok=False)
    save(out / "plan.json", {"collection": str(collection), "tasks": plan["tasks"], "delay_scale": 0,
                             "mode": "functional qualification only; no performance claim", "attempts_per_source": 1})
    results = []
    for key in plan["tasks"]:
        source = RUNS / (collection.name + "-" + key)
        summary = json.loads((source / "summary.json").read_text()) if (source / "summary.json").exists() else {}
        result = {"key": key, "source": str(source), "raw_audit_passed": False}
        if not summary.get("trace_complete", summary.get("pilot_valid", False)):
            result["status"] = "source_incomplete_not_replayed"
        else:
            name = replay_run_name(args.name + "-" + key, source, RUNS)
            run = RUNS / name
            print("START " + key, flush=True)
            with (out / (key + ".log")).open("w") as log:
                proc = subprocess.run([sys.executable, str(HERE / "run_pilot.py"), "--name", name,
                                       "--replay-source", str(source), "--delay-scale", "0"],
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            replay_summary = json.loads((run / "summary.json").read_text()) if (run / "summary.json").exists() else {}
            result.update(qualification=str(run), process_returncode=proc.returncode,
                          mismatches=[p.name for p in run.glob("mismatch-*")], status="qualification_failed")
            if replay_summary.get("trace_complete"):
                raw = audit(run)
                save(run / "replay-audit.json", raw)
                result["raw_audit_passed"] = raw["raw_evidence_matches"]
                if result["raw_audit_passed"]:
                    result["status"] = "qualified"
        save(source / "replay-eligibility.json", {"eligible": result["raw_audit_passed"],
                                                "reason": result["status"], "qualification": result.get("qualification")})
        results.append(result)
        save(out / "results.json", results)
        print("DONE " + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
