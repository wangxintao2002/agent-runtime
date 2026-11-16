import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from replay import Normalize, ReplayServer, response_frames, invokes_pytest


class ReplayTests(unittest.TestCase):
    def test_pytest_timing_in_compound_commands_preserves_actual_results(self):
        for command in ['pwd && python -m pytest -q tests',
                        'python -m pytest tests && python - <<\'PY\'\nprint(1)\nPY',
                        'python -c "print(1); print(2)"; python -m pytest -q tests',
                        'env PYTHONHASHSEED=0 /venv/bin/python3.9 -m pytest tests',
                        'pytest -q tests', 'python -m pytest tests 2>&1 | tail -20']:
            self.assertTrue(invokes_pytest(command), command)
            n = Normalize('/old/workspace', '/new/workspace', {'t': {
                'tool_name':'Bash', 'tool_input': {'command':command}}})
            self.assertEqual(n.output('3 passed in 0.30s', 't'), n.output('3 passed in 0.31s', 't'))
            self.assertNotEqual(n.output('3 passed in 0.30s', 't'), n.output('1 failed, 2 passed in 0.30s', 't'))
            self.assertEqual(n.output('value = 0.30s', 't'), 'value = 0.30s')
        for command in ['echo "python -m pytest tests"',
                        'python -c "print(\'python -m pytest tests\')"',
                        'echo ";" python -m pytest tests',
                        'cat <<EOF\npython -m pytest tests\nEOF',
                        'echo $(python -m pytest tests)']:
            self.assertFalse(invokes_pytest(command), command)

    def test_execution_date_only_in_cli_environment_and_only_known_dates(self):
        n = Normalize("/old/workspace", "/new/workspace", {}, runtime_dates=["2026-10-18", "2026-10-19"])
        prefix = "# Environment\nYou have been invoked in the following environment:\nother context\nToday's date is "
        def req(date, role="system", text_prefix=prefix):
            return {"messages": [{"role": role, "content": [{"type": "text", "text": text_prefix+date+"."}]}]}
        self.assertEqual(n.request(req("2026-10-18")), n.request(req("2026-10-19")))
        self.assertNotEqual(n.request(req("2026-10-18")), n.request(req("2026-10-20")))
        self.assertNotEqual(n.request(req("2026-10-18", "user")), n.request(req("2026-10-19", "user")))
        self.assertNotEqual(n.request(req("2026-10-18", text_prefix="date=")), n.request(req("2026-10-19", text_prefix="date=")))
        original = req("2026-10-18")
        n.request(original)
        self.assertIn("2026-10-18", original["messages"][0]["content"][0]["text"])
        strings = [{"messages": [{"role": "system", "content": prefix+d+"."}]} for d in ("2026-10-18", "2026-10-19")]
        self.assertEqual(n.request(strings[0]), n.request(strings[1]))

    def test_networkx_import_timestamp_keeps_version_and_results(self):
        normal = Normalize("/old/workspace", "/new/workspace", {"v": {"tool_name": "Bash",
            "tool_input": {"command": "python -c 'import networkx as nx; print(nx.__version__)'"}}})
        a = "version 2.0.dev_20261018090000\nVC {0, 1}"
        b = "version 2.0.dev_20261018100000\nVC {0, 1}"
        self.assertEqual(normal.output(a, "v"), normal.output(b, "v"))
        self.assertNotEqual(normal.output(a, "v"), normal.output(b.replace("2.0", "2.1"), "v"))
        self.assertNotEqual(normal.output(a, "v"), normal.output(b.replace("{0, 1}", "{3, 4}"), "v"))

    def test_complete_glob_order_and_duration_not_missing_files(self):
        calls = {"g": {"tool_name": "Glob", "tool_input": {"pattern": "*.py"}}}
        normal = Normalize("/old/workspace", "/new/workspace", calls, ["g"])
        a = {"filenames": ["a.py", "b.py"], "durationMs": 3, "truncated": False, "countIsComplete": True, "numFiles": 2}
        b = dict(a, filenames=["b.py", "a.py"], durationMs=10)
        self.assertEqual(normal.tool_response(a, "g"), normal.tool_response(b, "g"))
        self.assertNotEqual(normal.tool_response(a, "g"), normal.tool_response(dict(b, filenames=["b.py"]), "g"))
        self.assertNotEqual(normal.tool_response(a, "g"), normal.tool_response(dict(b, truncated=True), "g"))

    def test_normalization_does_not_hide_changed_results(self):
        calls = {"test": {"tool_name": "Bash", "tool_input": {"command": "python -m pytest -q"}},
                 "read": {"tool_name": "Read", "tool_input": {"file_path": "/old/workspace/a.py"}}}
        normal = Normalize("/old/workspace", "/new/workspace", calls)
        self.assertEqual(normal.output("50 passed in 0.32s", "test"), normal.output("50 passed in 1.11s", "test"))
        self.assertNotEqual(normal.output("50 passed in 0.32s", "test"), normal.output("49 passed in 0.32s", "test"))
        self.assertNotEqual(normal.output("x = 100", "read"), normal.output("x = 101", "read"))
        original = {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "test",
                    "content": "50 passed in 0.32s", "is_error": False}]}]}
        actual = copy.deepcopy(original)
        actual["messages"][0]["content"][0]["is_error"] = True
        self.assertNotEqual(normal.request(original), normal.request(actual))
        self.assertEqual(original["messages"][0]["content"][0]["content"], "50 passed in 0.32s")
        calls["test"]["tool_input"]["command"] = "python -m pytest -q tests/test_a.py 2>&1 | tail -20"
        self.assertEqual(normal.output("1 failed, 34 passed, 8 warnings in 0.32s", "test"),
                         normal.output("1 failed, 34 passed, 8 warnings in 1.25s", "test"))
        self.assertNotEqual(normal.output("1 failed, 34 passed in 0.32s", "test"),
                            normal.output("35 passed in 0.32s", "test"))
        self.assertEqual(normal.output("4 failed, 715 passed, 1 warning in 73.29s (0:01:13)", "test"),
                         normal.output("4 failed, 715 passed, 1 warning in 69.66s (0:01:09)", "test"))
        self.assertEqual(normal.output("<function foo at 0x1234>", "test"),
                         normal.output("<function foo at 0x4321>", "test"))
        self.assertNotEqual(normal.output("<function foo at 0x1234>", "test"),
                            normal.output("<function bar at 0x1234>", "test"))
        self.assertEqual(normal.output("limit = 0x1234", "test"), "limit = 0x1234")

    def source(self, root, tool=False):
        source = root / "source"
        folder = source / "requests/0001"
        folder.mkdir(parents=True)
        payload = {"model": "test", "messages": [], "stream": True}
        (source / "events.jsonl").write_text("")
        (folder / "request.json").write_text(json.dumps(payload))
        events = [{"type": "message_start", "message": {"id": "msg1"}}]
        if tool:
            argument = json.dumps({"file_path": str(source / "workspace/file.py")})
            split = argument.index("workspace") + 3
            events += [{"type": "content_block_delta", "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": part}}
                       for part in (argument[:split], argument[split:])]
        events.append({"type": "message_stop"})
        raw = b"".join(("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n").encode() for e in events)
        (folder / "response.bin").write_bytes(raw)
        # The wire chunks themselves split an SSE frame.
        (folder / "chunks.jsonl").write_text(json.dumps({"offset": 0, "length": 11, "monotonic_s": 10.1}) + "\n" +
            json.dumps({"offset": 11, "length": len(raw)-11, "monotonic_s": 10.2}) + "\n")
        (folder / "meta.json").write_text(json.dumps({"status": 200, "error": None, "start_s": 10,
                                                      "path": "/anthropic/v1/messages"}))
        return source, payload

    def test_split_json_path_remap_and_event_ready_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _ = self.source(root, tool=True)
            frames = response_frames(source / "requests/0001", source / "workspace", root / "new/workspace")
            self.assertTrue(all(abs(delay - .2) < 1e-9 for delay, _ in frames))
            args = ""
            for _, raw in frames:
                payload = json.loads(raw.decode().split("data: ", 1)[1])
                if payload.get("type") == "content_block_delta":
                    args += payload["delta"]["partial_json"]
            self.assertEqual(json.loads(args)["file_path"], str(root / "new/workspace/file.py"))

    def test_nonstream_token_count_is_replayed_as_original_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _ = self.source(root)
            folder = source / "requests/0001"
            raw = b'{"input_tokens":13457}'
            (folder / "request.json").write_text(json.dumps({"model": "test", "messages": []}))
            (folder / "response.bin").write_bytes(raw)
            (folder / "chunks.jsonl").write_text(json.dumps({"offset": 0, "length": len(raw), "monotonic_s": 10.2}) + "\n")
            frames = response_frames(folder, source / "workspace", root / "new/workspace")
            self.assertEqual(b"".join(raw for _, raw in frames), raw)
            self.assertAlmostEqual(frames[0][0], .2)

    def test_replay_has_no_upstream_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, payload = self.source(root)
            for mutate in (False, True):
                out = root / str(mutate)
                out.mkdir()
                server = ReplayServer(out, source, root / "workspace", delay_scale=0)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    request = dict(payload, model="wrong") if mutate else payload
                    with patch("http.client.HTTPSConnection", side_effect=AssertionError("No live model allowed")):
                        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                        client.request("POST", "/anthropic/v1/messages", json.dumps(request),
                                       {"Authorization": "Bearer " + server.token, "Content-Type": "application/json"})
                        response = client.getresponse()
                        self.assertEqual(response.status, 400 if mutate else 200)
                        response.read()
                        client.close()
                    self.assertEqual(server.failed.is_set(), mutate)
                    self.assertEqual(server.report()["upstream_requests"], 0)
                    self.assertEqual(server.report()["complete"], not mutate)
                finally:
                    server.shutdown()
                    server.server_close()

    def test_text_batch_flushes_before_next_source_event_and_keeps_controls(self):
        from audit_coalescing import audit_response
        from stream_coalesce import encode
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, payload = self.source(root)
            out = root / "coalesced"
            out.mkdir()
            server = ReplayServer(out, source, root / "workspace")
            server.coalesce_window_s = .02
            frames = [(0, encode("event: message_start", {"type": "message_start"})),
                (.001, encode("event: content_block_delta", {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": "hello"}})),
                (.005, encode("event: content_block_delta", {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": " world"}})),
                (.15, encode("event: message_stop", {"type": "message_stop"}))]
            server.frames = [frames]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                client.request("POST", "/anthropic/v1/messages", json.dumps(payload),
                               {"Authorization": "Bearer " + server.token, "Content-Type": "application/json"})
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                raw = response.read()
                client.close()
                folder = out / "requests/0001"
                # Close joins the request handler before reading its evidence.
                server.shutdown()
                server.server_close()
                meta = json.loads((folder / "meta.json").read_text())
                chunks = [json.loads(line) for line in (folder / "chunks.jsonl").read_text().splitlines()]
                self.assertEqual(len(chunks), 3)
                self.assertEqual(chunks[1]["source_event_indices"], [1, 2])
                self.assertLess(chunks[1]["monotonic_s"], chunks[2]["monotonic_s"]-.05)
                self.assertTrue(audit_response(frames, raw, chunks, meta["start_s"], 1, .02)["valid"])
            finally:
                server.shutdown()
                server.server_close()

    def test_grouping_ready_writes_preserves_all_sse_events_and_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, payload = self.source(root)
            out = root / "grouped"
            out.mkdir()
            server = ReplayServer(out, source, root / "workspace", delay_scale=0)
            server.group_ready_writes = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                client.request("POST", "/anthropic/v1/messages", json.dumps(payload),
                               {"Authorization": "Bearer " + server.token, "Content-Type": "application/json"})
                response = client.getresponse()
                raw = response.read()
                client.close()
                self.assertEqual(raw, b"".join(frame for _, frame in server.frames[0]))
            finally:
                server.shutdown()
                server.server_close()
            folder = out / "requests/0001"
            meta = json.loads((folder / "meta.json").read_text())
            chunks = [json.loads(line) for line in (folder / "chunks.jsonl").read_text().splitlines()]
            self.assertEqual(meta["wire_writes"], 1)
            self.assertEqual(len(chunks), 2)
            self.assertEqual(chunks[0]["monotonic_s"], chunks[1]["monotonic_s"])

    def test_changed_tool_output_stops_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _ = self.source(root)
            common = {"tool_use_id": "a", "tool_name": "Read", "tool_input": {"file_path": str(source / "workspace/x")}}
            pre = dict(common, hook_event_name="PreToolUse")
            post = dict(common, hook_event_name="PostToolUse", tool_response={"content": "original"})
            (source / "events.jsonl").write_text("".join(json.dumps({"payload": h}) + "\n" for h in (pre, post)))
            out = root / "out"
            out.mkdir()
            server = ReplayServer(out, source, root / "workspace", 0)
            try:
                server.allowed_tools.add("a")
                self.assertTrue(server.check_hook(pre))
                self.assertFalse(server.check_hook(dict(post, tool_response={"content": "changed"})))
                self.assertTrue(server.failed.is_set())
                self.assertFalse(server.report()["complete"])
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
