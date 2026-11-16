#!/usr/bin/env python3
"""Recheck stored requests/responses/tool evidence without trusting match counts."""
import argparse
import json
from pathlib import Path

from replay import Normalize, read_rows, response_frames
from tool_evidence import rejected_edits


def audit(run, allow_text_coalescing=False):
    """Incomplete evidence is a failed audit, including interrupted streams."""
    try:
        return _audit(run, allow_text_coalescing=allow_text_coalescing)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return dict(execution_evidence_matches=False, raw_evidence_matches=False,
                    audit_complete=False, stream_transform_authorized=allow_text_coalescing,
                    evidence_read_error=dict(type=type(exc).__name__, message=str(exc),
                                             file=getattr(exc, 'filename', None)))


def _audit(run, allow_text_coalescing=False):
    run = run.resolve()
    validation = json.loads((run / "replay-validation.json").read_text())
    configuration = json.loads((run / "metadata.json").read_text())
    source = Path(validation["source"])
    def hooks(root, kind):
        return {e["payload"]["tool_use_id"]: e["payload"] for e in read_rows(root / "events.jsonl")
                if e.get("payload", {}).get("hook_event_name") == kind}
    pre = hooks(source, "PreToolUse")
    post = {**hooks(source, "PostToolUse"), **hooks(source, "PostToolUseFailure")}
    actual_pre = hooks(run, "PreToolUse")
    actual_post = {**hooks(run, "PostToolUse"), **hooks(run, "PostToolUseFailure")}
    complete_globs = [k for k, h in post.items() if pre[k]["tool_name"] == "Glob"
                      and h.get("tool_response", {}).get("truncated") is False
                      and h.get("tool_response", {}).get("countIsComplete") is True]
    rejected = rejected_edits(read_rows(source / "claude.jsonl"), pre) if (source / "claude.jsonl").exists() else {}
    actual_rejected = rejected_edits(read_rows(run / "claude.jsonl"), actual_pre) if (run / "claude.jsonl").exists() else {}
    source_metadata = json.loads((source / "metadata.json").read_text()) if (source / "metadata.json").exists() else {}
    runtime_dates = [source_metadata.get("date_utc", "")[:10], configuration.get("date_utc", "")[:10]]
    normal = Normalize(source / "workspace", run / "workspace", {**pre, **rejected}, complete_globs, runtime_dates)
    rejection_ok = normal.paths(rejected) == normal.paths(actual_rejected)
    light = validation.get("executor") == "light"
    tools_ok = pre.keys() == actual_pre.keys() and post.keys() == actual_post.keys()
    if tools_ok:
        tools_ok = all(normal.paths(pre[k]["tool_input"]) == normal.paths(actual_pre[k]["tool_input"])
                       and pre[k]["tool_name"] == actual_pre[k]["tool_name"] for k in pre)
        normalize = normal.lightweight_response if light else normal.tool_response
        tools_ok = tools_ok and all(post[k]["hook_event_name"] == actual_post[k]["hook_event_name"]
                                     and normalize(post[k].get("tool_response", {}), k) ==
                                     normalize(actual_post[k].get("tool_response", {}), k) for k in post)
    expected_dirs = sorted((source / "requests").glob("[0-9][0-9][0-9][0-9]"))
    actual_dirs = sorted((run / "requests").glob("[0-9][0-9][0-9][0-9]"))
    requests_ok = len(expected_dirs) == len(actual_dirs)
    responses_ok = requests_ok
    lags = []
    transforms = []
    transport_checks = []
    response_equivalence_ok = requests_ok
    delivered_events = 0
    for index, (expected, actual) in enumerate(zip(expected_dirs, actual_dirs)):
        actual_request = json.loads((actual / "request.json").read_text())
        if light:
            requests_ok = requests_ok and actual_request == {"round": index+1, "stream": True}
        else:
            requests_ok = requests_ok and normal.request(json.loads((expected / "request.json").read_text())) == normal.request(actual_request)
        frames = response_frames(expected, source / "workspace", run / "workspace")
        raw = (actual / "response.bin").read_bytes()
        meta = json.loads((actual / "meta.json").read_text())
        declared_mode_matches = (
            bool(meta.get("group_ready_writes", False)) == bool(configuration.get("group_ready_writes", False))
            and meta.get("text_coalesce_window_s", 0) == configuration.get("text_coalesce_window_s", 0))
        responses_ok = responses_ok and declared_mode_matches
        response_equivalence_ok = response_equivalence_ok and declared_mode_matches
        chunks = read_rows(actual / "chunks.jsonl")
        delivered_events += len(chunks)
        if meta.get("group_ready_writes"):
            groups = 1+sum(a[0] != b[0] for a, b in zip(frames, frames[1:])) if frames else 0
            same_ready = len(chunks) == len(frames) and all(
                frames[i][0] != frames[i-1][0] or chunks[i]["monotonic_s"] == chunks[i-1]["monotonic_s"]
                for i in range(1, len(frames)))
            transport_check = dict(expected_ready_groups=groups, wire_writes=meta.get("wire_writes"),
                valid=meta.get("wire_writes") == groups and same_ready)
            transport_checks.append(transport_check)
            responses_ok = responses_ok and transport_check["valid"]
            response_equivalence_ok = response_equivalence_ok and transport_check["valid"]
        transformed = meta.get("text_coalesce_window_s", 0) > 0 and bool(json.loads((expected / "request.json").read_text()).get("stream"))
        if transformed:
            from audit_coalescing import audit_response
            result = audit_response(frames, raw, chunks, meta["start_s"], validation["delay_scale"], meta["text_coalesce_window_s"])
            transforms.append(result)
            response_equivalence_ok = response_equivalence_ok and allow_text_coalescing and result["valid"]
            responses_ok = False
            lags.append(result["excess_delay_max_s"])
        else:
            exact = b"".join(fragment for _, fragment in frames) == raw and len(chunks) == len(frames)
            responses_ok = responses_ok and exact
            response_equivalence_ok = response_equivalence_ok and exact
            for (delay, _), chunk in zip(frames, chunks):
                lags.append(chunk["monotonic_s"] - meta["start_s"] - delay * validation["delay_scale"])
    patches_ok = all((run / name).read_bytes() == (source / name).read_bytes()
                     for name in ("candidate.patch", "all-changes.patch"))
    timing_ok = bool(lags) and min(lags) >= -1e-6 and all(c["valid"] for c in transforms)
    acceptance_ok = (json.loads((run / "validation.json").read_text())["after"] ==
                     json.loads((source / "validation.json").read_text())["after"])
    return {"execution_evidence_matches": tools_ok and requests_ok and response_equivalence_ok and patches_ok and timing_ok and acceptance_ok and rejection_ok,
            "cli_environment_date_normalization": sorted(set(runtime_dates)),
            "transport_group_audits": transport_checks,
            "stream_transform_authorized": allow_text_coalescing,
            "stream_transform_audits": transforms,
            "raw_evidence_matches": tools_ok and requests_ok and responses_ok and patches_ok and timing_ok and acceptance_ok and rejection_ok,
            "rejected_edits_match": rejection_ok,
            "independent_acceptance_outcome_matches": acceptance_ok,
            "tool_inputs_and_outputs_match": tools_ok, "full_model_requests_match": requests_ok if not light else None,
            "model_round_sequence_matches": requests_ok, "executor": "light" if light else "claude",
            "all_replayed_response_bytes_match_remapped_source": responses_ok,
            "patches_match": patches_ok, "response_events_not_delivered_early": timing_ok,
            "event_schedule_lag_max_s": max(lags, default=None), "event_count": delivered_events}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--allow-text-coalescing", action="store_true", help="explicitly audit the declared text-only stream transform")
    args = parser.parse_args()
    result = audit(args.run.resolve(), allow_text_coalescing=args.allow_text_coalescing)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["execution_evidence_matches"] else 2)
