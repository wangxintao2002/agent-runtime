import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from record import Recorder
from summarize import partition, summarize


class RecorderTests(unittest.TestCase):
    def test_stream_forwarding_recording_and_key_boundary(self):
        received = []
        raw_response = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append((self.headers.get("Authorization"),
                                 self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw_response)))
                self.end_headers()
                self.wfile.write(raw_response)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                server = Recorder(out, "fake-upstream-key", max_requests=1,
                                  upstream=f"127.0.0.1:{upstream.server_port}", tls=False)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    body = b'{"model":"test","stream":true,"messages":[]}'
                    headers = {"Authorization": "Bearer " + server.token, "Content-Type": "application/json"}
                    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    client.request("POST", "/anthropic/v1/messages", body, headers)
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), raw_response)
                    client.close()
                    self.assertEqual(received, [("Bearer fake-upstream-key", body)])
                    # Wait for local record completion, not a guessed sleep.
                    with server.lock:
                        pass
                    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    client.request("POST", "/anthropic/v1/messages", body, headers)
                    response = client.getresponse()
                    self.assertEqual(response.status, 400)
                    response.read()
                    client.close()
                    self.assertEqual(len(received), 1)
                    self.assertEqual((out / "requests/0001/response.bin").read_bytes(), raw_response)
                    recorded = b"".join(p.read_bytes() for p in out.rglob("*") if p.is_file())
                    self.assertNotIn(b"fake-upstream-key", recorded)
                    self.assertNotIn(server.token.encode(), recorded)
                finally:
                    server.shutdown()
                    server.server_close()
        finally:
            upstream.shutdown()
            upstream.server_close()

    def test_overlapping_intervals_partition_not_double_counted(self):
        phases = partition(0, 10, [(1, 4), (3, 6)], [(5, 8)])
        totals = {p: sum(b - a for a, b, k in phases if k == p)
                  for p in ("model_only", "tool_only", "overlap", "other")}
        self.assertEqual(totals, {"model_only": 4, "tool_only": 2, "overlap": 1, "other": 3})
        self.assertEqual(sum(totals.values()), 10)

    def test_missing_kernel_peak_and_incomplete_stream(self):
        # Older kernels lack memory.peak; sampled peaks must stay distinguishable.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            def write(name, data):
                (out / name).write_text(json.dumps(data))
            def lines(name, data):
                (out / name).write_text("".join(json.dumps(x) + "\n" for x in data))
            stats = {"cpu.stat": {"usage_usec": 0}, "memory.peak": None, "memory.events": {"oom": 0},
                     **{x: {"some": {"total": "0"}} for x in ["cpu.pressure", "memory.pressure", "io.pressure"]}}
            write("window.json", {"start_s": 0, "end_s": 4, "before": stats, "after": stats, "agent_pid": 1})
            write("validation.json", {"independent_tests_pass": True, "agent_returncode": 0, "timed_out": False})
            write("observation-quality.json", {"valid": True, "gap_p95_s": .05, "gap_max_s": .05, "observer_thread_cpu_s": 0})
            lines("events.jsonl", [{"event": "hook", "monotonic_s": t,
                                    "payload": {"hook_event_name": kind, "tool_use_id": "call1", "tool_name": "Read"}}
                                   for t, kind in [(2, "PreToolUse"), (3, "PostToolUse")]])
            lines("resources.jsonl", [{"monotonic_s": t, "pool": {"memory.current": 2**20}}
                                      for t in range(5)])
            lines("claude.jsonl", [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "call1"}]}},
                                   {"type": "result", "is_error": False}])
            (out / "requests/0001").mkdir(parents=True)
            write("requests/0001/meta.json", {"request_id": 1, "start_s": 1, "end_s": 2,
                                              "status": 200, "error": None, "response_bytes": 20})
            write("requests/0001/request.json", {"stream": True})
            response = b"event: message_stop\n"
            (out / "requests/0001/response.bin").write_bytes(response)
            meta = json.loads((out / "requests/0001/meta.json").read_text())
            meta["response_bytes"] = len(response)
            write("requests/0001/meta.json", meta)
            lines("requests/0001/chunks.jsonl", [{"offset": 0, "length": len(response)}])
            summary = summarize(out)
            self.assertTrue(summary["pilot_valid"])
            self.assertIsNone(summary["cgroup_memory_peak_mib"])
            self.assertEqual(summary["cgroup_memory_sampled_peak_mib"], 1)
            (out / "prompt.txt").write_text("Fix the task")
            write("requests/0001/request.json", {"stream": True, "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Fix the task\naccidentally inherited stdin"}]}]})
            self.assertFalse(summarize(out)["trace_complete"])
            write("requests/0001/request.json", {"stream": True, "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Fix the task"}]}]})
            self.assertTrue(summarize(out)["trace_complete"])
            write("validation.json", {"independent_tests_pass": False, "agent_returncode": 0, "timed_out": False})
            failed_task = summarize(out)
            self.assertTrue(failed_task["trace_complete"])
            self.assertFalse(failed_task["pilot_valid"])
            self.assertFalse(failed_task["independent_tests_pass"])
            (out / "requests/0001/response.bin").write_bytes(b"incomplete")
            self.assertFalse(summarize(out)["pilot_valid"])
            self.assertFalse(summarize(out)["trace_complete"])


if __name__ == "__main__":
    unittest.main()
