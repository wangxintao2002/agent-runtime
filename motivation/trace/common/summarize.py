"""Partition observed wall time without adding overlapping API/tool intervals."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from tool_evidence import rejected_tools


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def partition(start, end, model, tools):
    boundaries = sorted({start, end, *(max(start, min(end, t))
                                      for interval in model + tools for t in interval)})
    intervals = []
    for a, b in zip(boundaries, boundaries[1:]):
        t = (a + b) / 2
        m = any(x <= t < y for x, y in model)
        u = any(x <= t < y for x, y in tools)
        phase = "overlap" if m and u else "model_only" if m else "tool_only" if u else "other"
        intervals.append((a, b, phase))
    return intervals


def summarize(out):
    metadata = json.loads((out / "metadata.json").read_text()) if (out / "metadata.json").exists() else {}
    executor = metadata.get("executor", "claude")
    events = rows(out / "events.jsonl")
    window = json.loads((out / "window.json").read_text())
    validation = json.loads((out / "validation.json").read_text())
    quality = json.loads((out / "observation-quality.json").read_text())
    records = [json.loads(p.read_text()) for p in sorted((out / "requests").glob("*/meta.json"))]
    model = [(r["start_s"], r["end_s"]) for r in records]
    begins, ends = {}, {}
    names = Counter()
    duplicate_ids = []
    for event in events:
        if event["event"] != "hook":
            continue
        payload = event["payload"]
        kind, call_id = payload["hook_event_name"], payload.get("tool_use_id")
        if kind == "PreToolUse":
            if call_id in begins:
                duplicate_ids.append(call_id)
            begins[call_id] = event["monotonic_s"]
            names[payload.get("tool_name")] += 1
        elif kind in ("PostToolUse", "PostToolUseFailure"):
            ends[call_id] = event["monotonic_s"]
    tools = [(begins[k], ends[k]) for k in begins.keys() & ends.keys()]
    phases = partition(window["start_s"], window["end_s"], model, tools)
    durations = {k: sum(b - a for a, b, p in phases if p == k)
                 for k in ("model_only", "tool_only", "overlap", "other")}
    samples = rows(out / "resources.jsonl")
    phase_memory = {}
    for phase in durations:
        integral = covered = 0
        memory_parts = {k: [0, 0] for k in ("anon", "file")}
        peak = 0
        for left, right in zip(samples, samples[1:]):
            memory = left["pool"]["memory.current"]
            for a, b, p in phases:
                if p == phase:
                    dt = max(0, min(b, right["monotonic_s"]) - max(a, left["monotonic_s"]))
                    integral += dt * memory
                    covered += dt
                    for key, pair in memory_parts.items():
                        value = (left["pool"].get("memory.stat") or {}).get(key)
                        if value is not None:
                            pair[0] += dt * value
                            pair[1] += dt
                    if dt:
                        peak = max(peak, memory)
        phase_memory[phase] = {"covered_s": covered, "mean_mib": integral / covered / 2**20 if covered else None,
                               "sampled_peak_mib": peak / 2**20 if covered else None,
                               **{f"mean_{k}_mib": total / duration / 2**20 if duration else None
                                  for k, (total, duration) in memory_parts.items()}}
    before, after = window["before"], window["after"]
    cpu = (after["cpu.stat"]["usage_usec"] - before["cpu.stat"]["usage_usec"]) / 1e6
    wall = window["end_s"] - window["start_s"]
    pressure = {resource: {level: (int(after[resource][level]["total"]) - int(before[resource][level]["total"])) / 1e6
                          for level in after[resource]}
                for resource in ("cpu.pressure", "memory.pressure", "io.pressure")}
    memory_events = {k: after["memory.events"][k] - before["memory.events"][k] for k in after["memory.events"]}
    messages = rows(out / ("light.jsonl" if executor == "light" else "claude.jsonl"))
    results = [m for m in messages if m.get("type") == ("light_result" if executor == "light" else "result")]
    stream_calls = {block["id"] for m in messages if m.get("type") == "assistant"
                    for block in m.get("message", {}).get("content", []) if block.get("type") == "tool_use"}
    if executor == "light":
        stream_calls = {m["id"] for m in messages if m.get("type") == "light_tool"}
    complete_tools = bool(begins) and begins.keys() == ends.keys() and None not in begins and not duplicate_ids
    rejected = rejected_tools(messages, begins) if executor == "claude" else {}
    complete_tools = complete_tools and stream_calls == begins.keys() | rejected.keys()
    fidelity = []
    for r in records:
        folder = out / "requests" / f"{r['request_id']:04d}"
        chunks = rows(folder / "chunks.jsonl")
        response = (folder / "response.bin").read_bytes() if (folder / "response.bin").exists() else b""
        offset, contiguous = 0, True
        for chunk in chunks:
            contiguous = contiguous and chunk["offset"] == offset
            offset += chunk["length"]
        request = json.loads((folder / "request.json").read_text())
        complete = not request.get("stream") or b'event: message_stop' in response
        fidelity.append(contiguous and offset == len(response) == r["response_bytes"] and complete)
    model_ok = bool(records) and all(r["status"] == 200 and r["error"] is None for r in records) and all(fidelity)
    prompt_matches = None
    if executor == "claude" and (out / "prompt.txt").exists() and records:
        first_request = json.loads((out / "requests/0001/request.json").read_text())
        # Preserve CRLF embedded in benchmark issue text; read_text translates it.
        intended = (out / "prompt.txt").read_bytes().decode("utf-8")
        prompt_matches = any(block.get("text") == intended for message in first_request.get("messages", [])
                             if message.get("role") == "user" and isinstance(message.get("content"), list)
                             for block in message["content"])
    agent_ok = len(results) == 1 and not results[0].get("is_error", True)
    environment_ok = not (out / "environment-invalid.json").exists()
    trace_complete = (validation["agent_returncode"] == 0
             and not validation["timed_out"] and quality["valid"] and complete_tools and model_ok and agent_ok
             and prompt_matches is not False and environment_ok)
    replay = json.loads((out / "replay-validation.json").read_text()) if (out / "replay-validation.json").exists() else None
    if replay:
        trace_complete = trace_complete and replay["complete"]
    valid = trace_complete and validation["independent_tests_pass"]
    sampled_memory = [s["pool"]["memory.current"] for s in samples
                      if window["start_s"] <= s["monotonic_s"] <= window["end_s"]]
    framework_rss = [p["memory_bytes"]["VmRSS"] for s in samples
                     for p in s.get("processes", {}).get("per_process_including_waited_children_do_not_sum", [])
                     if p["pid"] == window["agent_pid"] and "VmRSS" in p.get("memory_bytes", {})]
    return {"pilot_valid": valid, "trace_complete": trace_complete,
            "collection_eligible": trace_complete,
            "collection_policy": "Complete execution and trace; final answer/test correctness is not a selection criterion",
            "pilot_valid_legacy_definition": "Historical combined trace-and-test diagnostic; not collection eligibility",
            "executor": executor, "independent_tests_pass": validation["independent_tests_pass"], "replay_validation": replay,
            "wall_s": wall, "phase_wall_s": durations,
            "phase_definition": "HTTP request lifetime / pre-to-post tool hooks; includes transport and instrumentation; other is not pure framework overhead",
            "cpu_s": cpu, "mean_cpu_cores": cpu / wall,
            "cgroup_memory_peak_mib": after["memory.peak"] / 2**20 if after["memory.peak"] is not None else None,
            "cgroup_memory_sampled_peak_mib": max(sampled_memory) / 2**20 if sampled_memory else None,
            "agent_main_process_sampled_rss_peak_mib": max(framework_rss) / 2**20 if framework_rss else None,
            "phase_cgroup_memory": phase_memory, "pressure_s": pressure, "memory_event_delta": memory_events,
            "model_request_count": len(records), "model_http_status_counts": dict(Counter(r["status"] for r in records)),
            "model_route_counts": dict(Counter(r.get("path", "unknown") for r in records)),
            "model_records_ok": model_ok, "tool_counts": dict(names), "tool_hooks_complete": complete_tools,
            "recorded_prompt_matches_intended": prompt_matches,
            "task_environment_valid": environment_ok,
            "response_chunk_records_complete": all(fidelity),
            "unmatched_pre_tools": sorted(begins.keys() - ends.keys()), "unmatched_post_tools": sorted(ends.keys() - begins.keys()),
            "stream_tool_call_count": len(stream_calls), "agent_result_ok": agent_ok,
            "tools_rejected_without_hooks": {k: v["tool_name"] for k, v in sorted(rejected.items())},
            "edits_rejected_without_hooks": sorted(k for k, v in rejected.items() if v["tool_name"] == "Edit"),
            "rejected_edit_timing_note": "No tool boundary timestamps for these rejections; their resources remain in whole-task accounting",
            "usage_reported_by_cli": results[0].get("usage") if results else None,
            "cost_note": "Claude USD estimates are not treated as verified DeepSeek billing",
            "resource_observation_valid": quality["valid"], "sample_gap_p95_s": quality["gap_p95_s"],
            "process_snapshot_read_errors": quality.get("process_io_errors", {}),
            "process_snapshot_exit_races": quality.get("process_io_exit_races", 0),
            "sample_gap_max_s": quality["gap_max_s"], "observer_thread_cpu_s": quality["observer_thread_cpu_s"],
            "analysis_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    print(json.dumps(summarize(parser.parse_args().run), ensure_ascii=False, indent=2))
