#!/usr/bin/env python3
"""Per-recording resource evidence; signals are observations, not bottleneck claims."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))
from acceptance_evidence import assess


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def rows(path):
    if path.exists():
        with path.open() as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


def analyze(run):
    run = run.resolve()
    summary = read(run / "summary.json", {})
    validation = read(run / "validation.json", {})
    metadata = read(run / "metadata.json", {})
    samples = list(rows(run / "resources.jsonl"))
    window = read(run / "window.json", {})
    start, end = window.get("start_s"), window.get("end_s")
    measured = [s for s in samples if start is None or end is None or start <= s["monotonic_s"] <= end]
    attempts = list((run / "requests").glob("*/request.json"))
    events = list(rows(run / "events.jsonl"))
    complete = summary.get("trace_complete", summary.get("pilot_valid", False))
    state = ("complete_correct" if summary.get("independent_tests_pass") else "complete_model_failure") if complete else (
        "online_incomplete_needs_review" if attempts else "pre_online_failure_or_not_started")
    result = {"schema_version": 1, "analyzer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "run": str(run), "task": metadata.get("task"), "status": state,
        "http_requests_recorded": len(attempts), "trace_complete": complete,
        "correct": summary.get("independent_tests_pass"), "acceptance": validation.get("after"),
        "replay_eligibility": read(run / "replay-eligibility.json", {"status": "not_checked_by_this_analyzer"}),
        "resource_observation_valid": summary.get("resource_observation_valid", False),
        "wall_s": summary.get("wall_s"), "cpu_s": summary.get("cpu_s"),
        "phase_wall_s": summary.get("phase_wall_s"), "phase_cgroup_memory": summary.get("phase_cgroup_memory"),
        "pressure_s": summary.get("pressure_s"), "memory_event_delta": summary.get("memory_event_delta"),
        "hooked_tool_counts": summary.get("tool_counts"),
        "rejected_edits_without_hooks": summary.get("edits_rejected_without_hooks", []),
        "interpretation": "Single-task demand and pressure observations; no causal bottleneck or concurrent capacity claim. Process RSS is never summed."}
    result["local_recording_request_budget_exhausted"] = any(e.get("event") == "request_limit_reached" for e in events)
    result["trace_checks"] = {k: summary.get(k) for k in ["model_records_ok", "agent_result_ok", "tool_hooks_complete", "recorded_prompt_matches_intended", "task_environment_valid"]}
    if not complete and result["local_recording_request_budget_exhausted"]:
        result["status"] = "online_truncated_by_recording_budget"
    begins, durations = {}, []
    for e in sorted(events, key=lambda e: e["monotonic_s"]):
        h = e.get("payload", {})
        call_id = h.get("tool_use_id")
        if h.get("hook_event_name") == "PreToolUse":
            begins[call_id] = e
        elif h.get("hook_event_name") in ("PostToolUse", "PostToolUseFailure") and call_id in begins:
            begin = begins.pop(call_id)
            durations.append({"id": call_id, "name": h["tool_name"], "wall_s": e["monotonic_s"] - begin["monotonic_s"],
                              "input": begin["payload"]["tool_input"]})
    result["longest_hooked_tools"] = sorted(durations, key=lambda x: x["wall_s"], reverse=True)[:5]
    result["tool_timing_note"] = "Pre/post hook elapsed durations include instrumentation and may overlap; do not sum as task wall time"
    spec = read(run / "task-spec.json")
    if validation and spec and (run / "acceptance-after.log").exists():
        task = read(Path(spec["task_file"]))
        evidence = assess(task, (run / "acceptance-before.log").read_text(), (run / "acceptance-after.log").read_text(),
                          validation["before"]["returncode"], validation["after"]["returncode"])
        result["benchmark_acceptance"] = evidence
        if complete and evidence["observed_scope_correct"] and not evidence["declared_regression_coverage_complete"]:
            result["status"] = "complete_scope_correct_benchmark_coverage_incomplete"
        elif complete and not evidence["baseline_valid"]:
            result["status"] = "complete_baseline_needs_review"
        elif complete and evidence["benchmark_correct"] and not summary.get("independent_tests_pass"):
            result["status"] = "complete_benchmark_correct_with_known_failures"
    memory_samples = [s for s in measured if s.get("pool", {}).get("memory.current") is not None]
    result["missing_memory_samples"] = len(measured) - len(memory_samples)
    if memory_samples:
        peak = max(memory_samples, key=lambda x: x["pool"]["memory.current"])
        result["peak_mib"] = peak["pool"]["memory.current"] / 2**20
        # The independent observer moves slow memory.stat reads to diagnostics.
        # A missing simultaneous breakdown is unknown, never zero or a crash.
        breakdown = peak["pool"].get("memory.stat") or {}
        result["peak_anon_mib"] = breakdown["anon"] / 2**20 if "anon" in breakdown else None
        result["peak_file_mib"] = breakdown["file"] / 2**20 if "file" in breakdown else None
        pids = [x["pool"]["pids.current"] for x in measured if x["pool"].get("pids.current") is not None]
        result["max_tasks_including_threads"] = max(pids, default=None)
        intervals = []
        if start is not None and end is not None:
            for a, b in zip(samples, samples[1:]):
                if a.get("pool", {}).get("memory.current") is None:
                    continue
                dt = max(0, min(b["monotonic_s"], end) - max(a["monotonic_s"], start))
                intervals.append((dt, a["pool"]["memory.current"] / 2**20))
        covered = sum(dt for dt, _ in intervals)
        result["memory_sample_coverage_s"] = covered
        result["memory_mib_seconds"] = sum(dt * memory for dt, memory in intervals)
        result["mean_sampled_memory_mib"] = result["memory_mib_seconds"] / covered if covered else None
        result["time_above_mib_s"] = {str(limit): sum(dt for dt, memory in intervals if memory >= limit)
            for limit in [256, 512, 1024, 2048, 4096]}
        result["threshold_note"] = "Descriptive reporting bins only; never admission limits or bottleneck labels"
        proc_samples = [x for x in measured if x.get("processes")]
        if proc_samples:
            near = min(proc_samples, key=lambda x: abs(x["monotonic_s"] - peak["monotonic_s"]))
            processes = near["processes"]["per_process_including_waited_children_do_not_sum"]
            result["near_peak_processes"] = {"time_offset_s": near["monotonic_s"] - peak["monotonic_s"],
                "process_count": near["processes"]["live_processes_seen"],
                "names": dict(Counter(p.get("comm", "unknown") for p in processes)),
                "largest_individual_rss_mib": sorted([
                    {"comm": p.get("comm"), "rss_mib": p.get("memory_bytes", {}).get("VmRSS", 0) / 2**20}
                    for p in processes], key=lambda x: x["rss_mib"], reverse=True)[:5]}
        active = {}
        for event in sorted(events, key=lambda e: e["monotonic_s"]):
            if event["monotonic_s"] > peak["monotonic_s"]:
                continue
            h = event.get("payload", {})
            if h.get("hook_event_name") == "PreToolUse":
                active[h["tool_use_id"]] = {"name": h["tool_name"], "input": h["tool_input"]}
            elif h.get("hook_event_name") in ("PostToolUse", "PostToolUseFailure"):
                active.pop(h["tool_use_id"], None)
        result["active_tools_at_peak"] = active
    (run / "resource-profile.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.runs:
        result = analyze(path)
        print(json.dumps({k: result.get(k) for k in ["run", "status", "peak_mib", "mean_sampled_memory_mib", "max_tasks_including_threads"]}))
