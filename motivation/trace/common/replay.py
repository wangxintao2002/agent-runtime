"""Fail-closed, fixed-path replay through real Claude Code; no upstream client.

Actual tool responses stay in Claude's history. Only comparison copies are
normalized. A changed result never gets replaced with the recorded result.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import threading
import time

from record import Handler, Recorder, save
from tool_evidence import rejected_tools


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def invokes_pytest(command):
    """Recognize direct pytest commands, including simple shell command lists.

    Keep quotes while lexing so printed Python/shell snippets are not mistaken
    for executable commands. Unsupported substitutions/heredocs fail closed.
    """
    # Preserve the existing direct-command rule, including later shell lines.
    if command.startswith('python -m pytest '):
        return True
    if any(token in command for token in ('$(', '`', '<<', '\n')):
        return False
    try:
        lexer = shlex.shlex(command, posix=False, punctuation_chars=';&|')
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return False
    segment = []
    for token in [*words, ';']:
        if token not in (';', '&&', '||', '|'):
            segment.append(token)
            continue
        if segment and segment[0] == 'env':
            segment = segment[1:]
        while segment and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', segment[0]):
            segment = segment[1:]
        if segment:
            executable = Path(segment[0]).name
            if executable in ('pytest', 'py.test') or (
                re.fullmatch(r'python(?:\d+(?:\.\d+)?)?', executable)
                and segment[1:3] == ['-m', 'pytest']):
                return True
        segment = []
    return False


class Normalize:
    def __init__(self, original, workspace, calls, complete_globs=(), runtime_dates=()):
        self.original, self.workspace, self.calls = str(original), str(workspace), calls
        self.complete_globs = set(complete_globs)
        self.runtime_dates = set(runtime_dates)
        self.relative_workspaces = []
        for path in (Path(original), Path(workspace)):
            for parent in path.parents:
                if any((parent / name).is_file() for name in ("pytest.ini", "pyproject.toml", "setup.cfg")):
                    relative = str(path.relative_to(parent))
                    if "/" in relative:
                        self.relative_workspaces.append(relative)

    def paths(self, value):
        if isinstance(value, str):
            for workspace in (self.original, self.workspace):
                value = value.replace(str(Path(workspace).parent / "profile"), "<PROFILE>")
                value = value.replace(re.sub(r"[^a-zA-Z0-9]", "-", workspace), "<PROJECT_ID>")
                value = value.replace(workspace, "<WORKSPACE>")
            for relative in sorted(self.relative_workspaces, key=len, reverse=True):
                value = value.replace(relative + "/", "<WORKSPACE>/")
            return value
        if isinstance(value, list):
            return [self.paths(x) for x in value]
        if isinstance(value, dict):
            return {k: self.paths(v) for k, v in value.items()}
        return value

    def output(self, text, call_id):
        call = self.calls[call_id]
        text = self.paths(text)
        if not isinstance(text, str):
            return text
        if call["tool_name"] == "Grep":
            # ripgrep filesystem traversal order is unspecified; preserve each line/count.
            return "\n".join(sorted(text.splitlines()))
        if call["tool_name"] == "Glob" and call_id in self.complete_globs:
            return "\n".join(sorted(text.splitlines()))
        command = call["tool_input"].get("command", "")
        if (call["tool_name"] == "Bash" and "import networkx" in command and "__version__" in command):
            # This historical NetworkX checkout synthesizes a development version
            # from import time. Preserve version prefix and module origin.
            text = re.sub(r"\b(2\.0\.dev_)\d{14}\b", r"\1<IMPORT_TIMESTAMP>", text)
        if call["tool_name"] == "Bash" and command.startswith("ls -la "):
            # Only ls listing rows, never arbitrary dates/numbers in source text.
            text = re.sub(r"(?m)^([dl-][rwxstST-]{9}\s+\d+\s+\S+\s+\S+\s+\d+)\s+"
                          r"[A-Z][a-z]{2}\s+\d+\s+(?:\d{2}:\d{2}|\d{4})\s+", r"\1 <MTIME> ", text)
        if call["tool_name"] == "Bash" and invokes_pytest(command):
            # Preserve every result count/status; normalize only pytest's final duration.
            text = re.sub(r"(?m)^(=+ )?((?:\d+ (?:passed|failed|skipped|xfailed|xpassed|deselected|errors?|warnings?))(?:, \d+ (?:passed|failed|skipped|xfailed|xpassed|deselected|errors?|warnings?))*) in \d+(?:\.\d+)?s(?: \(\d+:\d{2}:\d{2}\))?( =+)?$",
                          r"\1\2 in <SECONDS>s\3", text)
        if call["tool_name"] == "Bash" and command.startswith("python "):
            text = re.sub(r"(<(?:function [^\n]+?|[\w.]+(?:\.<locals>\.[\w.]+)? object) at )0x[0-9a-fA-F]+(>)", r"\1<ADDRESS>\2", text)
        return text

    def tool_response(self, response, call_id):
        result = self.paths(response)
        if self.calls[call_id]["tool_name"] == "Bash":
            result["stdout"] = self.output(result.get("stdout", ""), call_id)
        if self.calls[call_id]["tool_name"] == "Grep":
            result["content"] = self.output(result.get("content", ""), call_id)
            if result.get("mode") == "files_with_matches" and result.get("numFiles") == result.get("totalFiles"):
                # Full ripgrep filename results have unspecified traversal order.
                result["filenames"] = sorted(result.get("filenames", []))
        if self.calls[call_id]["tool_name"] == "Glob":
            # Runtime telemetry, not a filename/result; keep file list and counts.
            result.pop("durationMs", None)
            if call_id in self.complete_globs and result.get("truncated") is False and result.get("countIsComplete") is True:
                result["filenames"] = sorted(result["filenames"])
        return result

    def lightweight_response(self, response, call_id):
        result = self.tool_response(response, call_id)
        if self.calls[call_id]["tool_name"] == "Edit":
            # Diff display formatting is not a file effect. Original file,
            # replacement strings/options and final Git patch remain checked.
            result.pop("structuredPatch", None)
        return result

    def request(self, request):
        result = self.paths(copy.deepcopy(request))
        # These are instance identifiers, not task/model parameters.
        if "user_id" in result.get("metadata", {}):
            identity = json.loads(result["metadata"]["user_id"])
            for key in ("session_id", "device_id"):
                if key in identity:
                    identity[key] = "<INSTANCE>"
            result["metadata"]["user_id"] = json.dumps(identity, sort_keys=True)
        for message in result.get("messages", []):
            content = message.get("content")
            if (message.get("role") == "system" and isinstance(content, str)
                and content.startswith("# Environment\nYou have been invoked in the following environment:")):
                message["content"] = re.sub(r"Today's date is (\d{4}-\d{2}-\d{2})\.$",
                    lambda m: "Today's date is <EXECUTION_DATE>." if m[1] in self.runtime_dates else m[0], content)
            if isinstance(content, list):
                for block in content:
                    if (message.get("role") == "system" and block.get("type") == "text"
                        and block.get("text", "").startswith("# Environment\nYou have been invoked in the following environment:")):
                        # Only the CLI-added environment trailer, never dates in
                        # user instructions, code, tool output or model responses.
                        block["text"] = re.sub(r"Today's date is (\d{4}-\d{2}-\d{2})\.$",
                            lambda m: "Today's date is <EXECUTION_DATE>." if m[1] in self.runtime_dates else m[0], block["text"])
                    if block.get("type") == "tool_result":
                        block["content"] = self.output(block["content"], block["tool_use_id"])
                if content and all(b.get("type") == "tool_result" for b in content):
                    content.sort(key=lambda b: b["tool_use_id"])
        return result


def response_frames(folder, original, workspace):
    """Keep SSE event availability times; remap split tool argument paths.

    TCP chunks may split JSON strings and paths. Reconstruct each tool argument
    string, remap it, then divide it across the SAME input-delta events. This
    preserves the event sequence; event delivery is scheduled at the source
    chunk which made that event complete, not at invented token boundaries.
    """
    raw = (folder / "response.bin").read_bytes()
    chunks = read_rows(folder / "chunks.jsonl")
    meta = json.loads((folder / "meta.json").read_text())
    if meta["status"] != 200 or meta["error"]:
        raise ValueError("Source response is incomplete or failed")
    expected_offset = 0
    for chunk in chunks:
        if chunk["offset"] != expected_offset:
            raise ValueError("Noncontiguous source chunks")
        expected_offset += chunk["length"]
    if expected_offset != len(raw):
        raise ValueError("Source chunk lengths do not cover response")
    request = json.loads((folder / "request.json").read_text())
    if not request.get("stream"):
        json.loads(raw)  # E.g. Claude's count_tokens endpoint; preserve exact JSON bytes.
        return [(max(0, c["monotonic_s"] - meta["start_s"]), raw[c["offset"]:c["offset"] + c["length"]]) for c in chunks]
    if not raw.endswith(b"\n\n"):
        raise ValueError("Source streaming response is incomplete")
    events, end = [], 0
    partials = {}
    for frame in raw.split(b"\n\n")[:-1]:
        end += len(frame) + 2
        ready = next(c["monotonic_s"] for c in chunks if c["offset"] + c["length"] >= end)
        lines = frame.decode().splitlines()
        payload = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        if payload.get("type") == "content_block_delta" and payload.get("delta", {}).get("type") == "input_json_delta":
            partials.setdefault(payload["index"], []).append(payload["delta"])
        events.append((max(0, ready - meta["start_s"]), lines[0], payload))
    for deltas in partials.values():
        text = "".join(d["partial_json"] for d in deltas)
        text = text.replace(str(original), str(workspace))
        json.loads(text)  # Fail before executing a malformed tool argument.
        offset = 0
        for i, delta in enumerate(deltas):
            size = len(delta["partial_json"])
            delta["partial_json"] = text[offset:] if i == len(deltas) - 1 else text[offset:offset + size]
            offset += size
    if not events or events[-1][2].get("type") != "message_stop":
        raise ValueError("Missing terminal message_stop")
    return [(delay, (head + "\ndata: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode())
            for delay, head, payload in events]


class ReplayServer(Recorder):
    def __init__(self, out, source, workspace, delay_scale=1, executor="claude"):
        self.group_ready_writes = False
        self.coalesce_window_s = 0
        self.executor = executor
        self.source, self.workspace = source, workspace
        self.original = source / "workspace"
        self.delay_scale = delay_scale
        self.failed = threading.Event()
        self.calls, self.expected_posts = {}, {}
        for event in read_rows(source / "events.jsonl"):
            hook = event.get("payload", {})
            if hook.get("hook_event_name") == "PreToolUse":
                self.calls[hook["tool_use_id"]] = hook
            if hook.get("hook_event_name") in ("PostToolUse", "PostToolUseFailure"):
                self.expected_posts[hook["tool_use_id"]] = hook
        if self.calls.keys() != self.expected_posts.keys():
            raise ValueError("Source tool events incomplete")
        self.rejected = rejected_tools(read_rows(source / "claude.jsonl"), self.calls) if (source / "claude.jsonl").exists() else {}
        if self.rejected and executor == "light":
            raise ValueError("Light executor does not implement Claude pre-hook tool rejection")
        self.calls.update(self.rejected)
        self.rejections_seen = set()
        complete_globs = [k for k, h in self.expected_posts.items() if self.calls[k]["tool_name"] == "Glob"
                          and h.get("tool_response", {}).get("truncated") is False
                          and h.get("tool_response", {}).get("countIsComplete") is True]
        metadata_path = source / "metadata.json"
        source_date = json.loads(metadata_path.read_text()).get("date_utc", "")[:10] if metadata_path.exists() else ""
        self.normal = Normalize(self.original, workspace, self.calls, complete_globs,
                                [source_date, datetime.now(timezone.utc).date().isoformat()])
        self.folders = sorted((source / "requests").glob("[0-9][0-9][0-9][0-9]"))
        self.frames = [response_frames(f, self.original, workspace) for f in self.folders]
        self.expected_requests = [json.loads((f / "request.json").read_text()) for f in self.folders]
        self.started_tools, self.finished_tools = set(), set()
        self.allowed_tools = set()
        self.hook_lock = threading.Lock()
        self.validated_requests = 0
        super().__init__(out, "", max_requests=len(self.folders), handler=ReplayHandler)
        source_files = [source / "events.jsonl", source / "claude.jsonl", source / "candidate.patch", source / "all-changes.patch"]
        source_files += [f / name for f in self.folders for name in ("request.json", "response.bin", "chunks.jsonl", "meta.json")]
        save(out / "replay-source-manifest.json", {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                                                   for p in source_files if p.exists()})

    def mismatch(self, kind, expected, actual):
        self.failed.set()
        self.event("replay_mismatch", kind=kind)
        save(self.out / ("mismatch-" + kind.replace("/", "_") + ".json"),
             {"expected": expected, "actual": actual})

    def check_hook(self, hook):
        kind, call_id = hook["hook_event_name"], hook.get("tool_use_id")
        if kind not in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            return True
        with self.hook_lock:
            if self.failed.is_set():
                return False
            expected = self.calls.get(call_id)
            if expected is None or call_id not in self.allowed_tools or call_id in self.rejected:
                self.mismatch("unexpected-tool", list(self.allowed_tools), call_id)
                return False
            fields = ("tool_name", "tool_input")
            a, b = ({k: self.normal.paths(h[k]) for k in fields} for h in (expected, hook))
            if a != b:
                self.mismatch("tool-input-" + call_id, a, b)
                return False
            if kind == "PreToolUse":
                if call_id in self.started_tools:
                    self.mismatch("duplicate-pre", None, call_id)
                    return False
                self.started_tools.add(call_id)
                return True
            expected = self.expected_posts[call_id]
            normalize = self.normal.lightweight_response if self.executor == "light" else self.normal.tool_response
            a, b = (normalize(h.get("tool_response", {}), call_id) for h in (expected, hook))
            if (call_id not in self.started_tools or call_id in self.finished_tools
                    or expected["hook_event_name"] != kind or a != b):
                self.mismatch("tool-output-" + call_id, a, b)
                return False
            self.finished_tools.add(call_id)
            return True

    def report(self):
        return {"source": str(self.source), "delay_scale": self.delay_scale,
                "executor": self.executor,
                "text_coalesce_window_s": self.coalesce_window_s,
                "upstream_requests": 0, "matched_requests": self.validated_requests,
                "expected_requests": len(self.folders), "started_tools": len(self.started_tools),
                "matched_tool_results": len(self.finished_tools), "expected_tools": len(self.expected_posts),
                "expected_rejected_tools": len(self.rejected), "matched_rejected_tools": len(self.rejections_seen),
                "expected_rejected_edits": sum(r["tool_name"] == "Edit" for r in self.rejected.values()),
                "matched_rejected_edits": sum(self.rejected[k]["tool_name"] == "Edit" for k in self.rejections_seen),
                "mismatch": self.failed.is_set(),
                "complete": not self.failed.is_set() and self.validated_requests == len(self.folders)
                            and self.finished_tools == self.expected_posts.keys() and self.rejections_seen == self.rejected.keys()}


class ReplayHandler(Handler):
    def do_POST(self):
        try:
            self.handle_replay()
        except Exception as exc:
            self.server.mismatch("handler-error", "valid recorded protocol", type(exc).__name__)
            self.close_connection = True
            try:
                self.reply(400, {"error": "Replay protocol validation failed"})
            except OSError:
                pass

    def handle_replay(self):
        server = self.server
        if self.headers.get("Authorization") != "Bearer " + server.token:
            self.reply(403, {"error": "local replay authentication failed"})
            return
        size = int(self.headers.get("Content-Length", 0))
        if not 0 < size <= 16 * 1024**2:
            self.reply(413, {"error": "invalid size"})
            return
        http_body_read_started = time.monotonic()
        payload = json.loads(self.rfile.read(size))
        payload_received = time.monotonic()
        if self.path == "/hooks":
            server.event("hook", payload=payload)
            ok = server.check_hook(payload)
            self.reply(200, {} if ok or payload["hook_event_name"] != "PreToolUse" else {
                "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": "Recorded execution path mismatch"}})
            return
        server.event("model_request_received", path=self.path, body_read_started_s=http_body_read_started,
                     payload_received_s=payload_received)
        validation_started = time.monotonic()
        if server.failed.is_set():
            self.reply(400, {"error": "Replay stopped after mismatch"})
            return
        with server.lock:
            index = server.request_count
            server.request_count += 1
        if index >= len(server.folders) or self.path != json.loads((server.folders[index] / "meta.json").read_text())["path"]:
            server.mismatch("unexpected-request", "recorded message route", self.path)
            self.reply(400, {"error": "Unexpected replay request"})
            return
        if server.executor == "light":
            a, b = {"round": index + 1, "stream": True}, payload
            preceding = server.expected_requests[index]["messages"]
            required = {block["tool_use_id"] for message in preceding if isinstance(message.get("content"), list)
                        for block in message["content"] if block.get("type") == "tool_result"}
            if not required <= server.finished_tools:
                server.mismatch("light-premature-next-round", sorted(required), sorted(server.finished_tools))
                self.reply(400, {"error": "Previous real tools are incomplete"})
                return
        else:
            a = server.normal.request(server.expected_requests[index])
            b = server.normal.request(payload)
        if a != b:
            server.mismatch(f"request-{index+1:04d}", a, b)
            self.reply(400, {"error": "Request differs from recorded execution"})
            return
        for message in payload.get("messages", []):
            for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
                if block.get("type") == "tool_result" and block["tool_use_id"] in server.rejected:
                    key = block["tool_use_id"]
                    expected = server.rejected[key]["error_result"]
                    if key not in server.allowed_tools or server.normal.paths(block) != server.normal.paths(expected):
                        server.mismatch("changed-tool-rejection", server.normal.paths(expected), server.normal.paths(block))
                        self.reply(400, {"error": "Tool rejection mismatch"})
                        return
                    server.rejections_seen.add(key)
                    continue
                if block.get("type") == "tool_result" and block["tool_use_id"] not in server.finished_tools:
                    server.mismatch("unverified-tool-result", "verified post hook", block["tool_use_id"])
                    self.reply(400, {"error": "Tool result was not independently checked"})
                    return
        # Tool IDs become executable when their own response block is served;
        # looking at the next request is wrong when count_tokens is interleaved.
        server.validated_requests += 1
        folder = server.out / "requests" / f"{index+1:04d}"
        folder.mkdir()
        save(folder / "request.json", payload)
        started, received, first_byte, error = time.monotonic(), 0, None, None
        server.event("model_request_start", request_id=index+1, replay=True)
        if index == 0:
            (server.out / "first-model-request").touch()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if server.expected_requests[index].get("stream") else "application/json")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        server.event("model_response_headers", request_id=index+1, status=200)
        lags = []
        wire_writes = 0
        try:
            with (folder / "response.bin").open("wb") as out, (folder / "chunks.jsonl").open("w") as chunks:
                coalescer = None
                if server.coalesce_window_s and server.expected_requests[index].get("stream"):
                    from stream_coalesce import TextCoalescer
                    coalescer = TextCoalescer(server.coalesce_window_s)
                def deliver_many(emissions):
                    nonlocal received, first_byte, wire_writes
                    now = time.monotonic()
                    if first_byte is None:
                        first_byte = now
                        server.event("model_first_byte", request_id=index+1)
                    for frame, source_indices in emissions:
                        out.write(frame)
                        record = {"monotonic_s": now, "offset": received, "length": len(frame)}
                        if coalescer:
                            record["source_event_indices"] = source_indices
                        chunks.write(json.dumps(record) + "\n")
                        received += len(frame)
                        if server.expected_requests[index].get("stream"):
                            event = json.loads(next(line[6:] for line in frame.decode().splitlines() if line.startswith("data: ")))
                            block = event.get("content_block", {})
                            if event.get("type") == "content_block_start" and block.get("type") == "tool_use":
                                server.allowed_tools.add(block["id"])
                    self.wfile.write(b"".join(frame for frame, _ in emissions))
                    self.wfile.flush()
                    wire_writes += 1
                def deliver(frame, source_indices):
                    deliver_many([(frame, source_indices)])
                def wait_until(target):
                    if server.failed.wait(max(0, target-time.monotonic())):
                        raise RuntimeError("Replay stopped")
                wire_pending, wire_due = [], None
                for source_index, (delay, frame) in enumerate(server.frames[index]):
                    if wire_pending and delay != wire_due:
                        deliver_many(wire_pending)
                        wire_pending = []
                    target = started + delay * server.delay_scale
                    if coalescer and coalescer.deadline is not None and coalescer.deadline < target:
                        wait_until(coalescer.deadline)
                        for emitted, indices in coalescer.flush():
                            deliver(emitted, indices)
                    wait_until(target)
                    now = time.monotonic()
                    lags.append(max(0, now-target))
                    emissions = coalescer.feed(frame, now, source_index) if coalescer else [(frame, [source_index])]
                    if server.group_ready_writes:
                        wire_pending.extend(emissions)
                        wire_due = delay
                    else:
                        for emitted, indices in emissions:
                            deliver(emitted, indices)
                if wire_pending:
                    deliver_many(wire_pending)
                if coalescer:
                    for emitted, indices in coalescer.flush():
                        deliver(emitted, indices)
        except Exception as exc:
            error = type(exc).__name__
            server.mismatch("stream-" + str(index+1), "complete stream", error)
        finally:
            meta = {"request_id": index+1, "path": self.path, "start_s": started, "end_s": time.monotonic(),
                    "first_byte_s": first_byte, "status": 200, "error": error, "response_bytes": received,
                    "source_request_sha256": hashlib.sha256((server.folders[index] / "request.json").read_bytes()).hexdigest(),
                    "replay_schedule_lag_max_s": max(lags, default=0),
                    "text_coalesce_window_s": server.coalesce_window_s,
                    "group_ready_writes": server.group_ready_writes, "wire_writes": wire_writes,
                    "body_read_s": payload_received - http_body_read_started,
                    "validation_and_record_setup_s": started - validation_started}
            save(folder / "meta.json", meta)
            server.event("model_request_end", **meta)
