"""Bounded streaming text batching; no task identity or future input required.

Only adjacent text/thinking deltas with identical envelopes can be combined.
Tool JSON, signatures, control events, and unknown extensions pass unchanged.
The caller must flush at deadline even if no more bytes arrive.
"""
import copy
import json


def decode(frame):
    lines = frame.decode().splitlines()
    if len(lines) != 3 or not lines[0].startswith("event: ") or not lines[1].startswith("data: ") or lines[2]:
        return None
    try:
        payload = json.loads(lines[1][6:])
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return lines[0], payload


def text_key(payload):
    if payload.get("type") != "content_block_delta":
        return None
    delta = payload.get("delta", {})
    if not isinstance(delta, dict):
        return None
    field = {"text_delta": "text", "thinking_delta": "thinking"}.get(delta.get("type"))
    if field and set(delta) == {"type", field} and isinstance(delta[field], str):
        return field
    return None


def encode(header, payload):
    return (header + "\ndata: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()


class TextCoalescer:
    def __init__(self, window_s=.02, max_bytes=16384, max_events=256):
        if not 0 < window_s <= .1 or max_bytes < 1 or max_events < 1:
            raise ValueError("Invalid bounded text batching limits")
        self.window_s, self.max_bytes = window_s, max_bytes
        self.max_events = max_events
        self.pending = None
        self.deadline = None

    def flush(self):
        if self.pending is None:
            return []
        pending = self.pending
        self.pending = self.deadline = None
        return [(pending["frame"], pending["indices"])]

    def feed(self, frame, now, source_index):
        emitted = []
        if self.deadline is not None and now >= self.deadline:
            emitted += self.flush()
        decoded = decode(frame)
        field = text_key(decoded[1]) if decoded else None
        if not field:
            return emitted + self.flush() + [(frame, [source_index])]
        header, payload = decoded
        envelope = copy.deepcopy(payload)
        envelope["delta"][field] = ""
        if self.pending is not None:
            previous = self.pending
            if previous["header"] == header and previous["envelope"] == envelope:
                combined = copy.deepcopy(payload)
                combined["delta"][field] = previous["payload"]["delta"][field] + payload["delta"][field]
                raw = encode(header, combined)
                if len(raw) <= self.max_bytes and len(previous["indices"]) < self.max_events:
                    previous.update(frame=raw, payload=combined)
                    previous["indices"].append(source_index)
                    return emitted
            emitted += self.flush()
        if len(frame) >= self.max_bytes:
            return emitted + [(frame, [source_index])]
        self.pending = dict(frame=frame, indices=[source_index], header=header,
                            payload=payload, envelope=envelope)
        self.deadline = now + self.window_s
        return emitted
