#!/usr/bin/env python3
"""Bounded sequential online recording, preserving every attempt and failure."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from run_pilot import HERE, RUNS, save
from analyze_recording import analyze
from task_spec import load_spec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--tasks", nargs="+", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.name) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", k) for k in args.tasks):
        parser.error("Invalid name/task key")
    specs = [HERE / "task-specs" / (k + ".json") for k in args.tasks]
    identities = [json.loads(Path(load_spec(p)["task_file"]).read_text())["instance_id"] for p in specs]
    if len(set(identities)) != len(identities):
        parser.error("Task identities must be distinct")
    out = RUNS / args.name
    out.mkdir(parents=True, exist_ok=False)
    save(out / "plan.json", {"tasks": args.tasks, "identities": identities, "attempts_per_task": 1,
                             "online_concurrency": 1, "timeout_s": 600, "max_requests": 40,
                             "stdin": "DEVNULL", "retry_for_model_failure": False})
    results = []
    for key, spec in zip(args.tasks, specs):
        name = args.name + "-" + key
        print("START " + name, flush=True)
        with (out / (key + ".log")).open("w") as log:
            proc = subprocess.run([sys.executable, str(HERE / "run_pilot.py"), "--name", name,
                                   "--task-spec", str(spec)], stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=subprocess.STDOUT)
        summary = RUNS / name / "summary.json"
        result = json.loads(summary.read_text()) if summary.exists() else {}
        try:
            profile = analyze(summary.parent)
            analysis = {"profile": str(summary.parent / "resource-profile.json"), "analysis_status": profile["status"],
                        "benchmark_correct": profile.get("benchmark_acceptance", {}).get("benchmark_correct")}
        except Exception as exc:
            # Preserve the collection outcome even if post-processing fails.
            analysis = {"analysis_error": type(exc).__name__}
        results.append({"key": key, "run": str(summary.parent), "returncode": proc.returncode,
                        "trace_complete": result.get("trace_complete", False),
                        "correct": result.get("independent_tests_pass", False),
                        "model_requests": result.get("model_request_count", 0), **analysis})
        save(out / "results.json", results)
        print("DONE " + json.dumps(results[-1]), flush=True)


if __name__ == "__main__":
    main()
