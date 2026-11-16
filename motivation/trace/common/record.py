"""Local observation endpoint; proxy bodies unchanged, never record auth headers.

The server runs OUTSIDE the measured Agent cgroup. This is a single-pilot
recorder, not yet a validated high-concurrency replay server.
"""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import uuid


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class Recorder(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, out, key, max_requests=40, upstream="api.deepseek.com", tls=True, handler=None):
        self.out, self.key = out, key
        self.upstream, self.tls = upstream, tls
        self.max_requests = max_requests
        self.token = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.request_count = 0
        self.active = 0
        (out / "requests").mkdir()
        super().__init__(("127.0.0.1", 0), handler or Handler)
        self.url = f"http://127.0.0.1:{self.server_port}"

    def event(self, event, **fields):
        with self.lock:
            with (self.out / "events.jsonl").open("a") as f:
                f.write(json.dumps({"monotonic_s": time.monotonic(), "event": event,
                                    **fields}, ensure_ascii=False) + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # No request headers, URLs or auth in console logs.

    def reply(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        server = self.server
        if self.headers.get("Authorization") != "Bearer " + server.token:
            self.reply(403, {"error": "local recorder authentication failed"})
            return
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 16 * 1024**2:
            self.reply(413, {"error": "invalid body size"})
            return
        body = self.rfile.read(size)
        try:
            payload = json.loads(body)
        except ValueError:
            self.reply(400, {"error": "invalid JSON"})
            return
        if self.path == "/hooks":
            server.event("hook", payload=payload)
            self.reply(200, {})
            return
        if self.path.split("?")[0] not in {"/anthropic/v1/messages", "/anthropic/v1/messages/count_tokens"}:
            self.reply(404, {"error": "unsupported API path"})
            return
        with server.lock:
            if server.request_count >= server.max_requests:
                over_limit = True
            else:
                over_limit = False
                server.request_count += 1
                number = server.request_count
                server.active += 1
        if over_limit:
            server.event("request_limit_reached")
            self.reply(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                        "message": "Pilot request limit reached"}})
            return
        folder = server.out / "requests" / f"{number:04d}"
        folder.mkdir()
        (folder / "request.json").write_bytes(body)
        started = time.monotonic()
        server.event("model_request_start", request_id=number, path=self.path,
                     model=payload.get("model"), request_bytes=len(body))
        connection = None
        status, first_byte, received, error = None, None, 0, None
        sent_headers = False
        try:
            cls = http.client.HTTPSConnection if server.tls else http.client.HTTPConnection
            connection = cls(server.upstream, timeout=180)
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() in {"content-type", "anthropic-version", "anthropic-beta", "user-agent"}}
            headers["Authorization"] = "Bearer " + server.key
            headers["Accept-Encoding"] = "identity"
            connection.request("POST", self.path, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            server.event("model_response_headers", request_id=number, status=status)
            self.send_response(status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            sent_headers = True
            with (folder / "response.bin").open("wb") as output, (folder / "chunks.jsonl").open("w") as chunks:
                while chunk := response.read1(65536):
                    now = time.monotonic()
                    if first_byte is None:
                        first_byte = now
                        server.event("model_first_byte", request_id=number)
                    output.write(chunk)
                    chunks.write(json.dumps({"monotonic_s": now, "offset": received,
                                             "length": len(chunk)}) + "\n")
                    received += len(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except Exception as exc:
            error = type(exc).__name__
            if not sent_headers:
                self.reply(502, {"error": "upstream recorder failure"})
        finally:
            if connection:
                connection.close()
            ended = time.monotonic()
            meta = {"request_id": number, "path": self.path, "start_s": started, "end_s": ended,
                    "first_byte_s": first_byte, "status": status, "response_bytes": received,
                    "error": error}
            save(folder / "meta.json", meta)
            server.event("model_request_end", **meta)
            with server.lock:
                server.active -= 1
