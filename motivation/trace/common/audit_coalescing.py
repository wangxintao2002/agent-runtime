"""Independent evidence audit: exact content, source coverage and bounded delay.

This is an explicit transformed-stream audit, not byte-identical replay.
No imports from the batching implementation.
"""
import copy
import json


def parse(frame):
    lines = frame.decode().splitlines()
    if len(lines) != 3 or lines[2] or not lines[0].startswith("event: ") or not lines[1].startswith("data: "):
        raise ValueError("Unexpected SSE envelope")
    return lines[0], json.loads(lines[1][6:])


def audit_response(frames, raw, chunks, started, scale, window):
    errors, lags, delays = [], [], []
    cursor = offset = 0
    try:
        if not 0 < window <= .1:
            raise ValueError("Invalid declared buffering window")
        for chunk in chunks:
            indices = chunk["source_event_indices"]
            if not indices or indices != list(range(cursor, cursor+len(indices))):
                raise ValueError("Source events reordered, dropped or duplicated")
            if len(indices) > 256:
                raise ValueError("Combined event exceeds event-count bound")
            if indices[-1] >= len(frames) or chunk["offset"] != offset:
                raise ValueError("Invalid stream coverage")
            if not isinstance(chunk["length"], int) or chunk["length"] <= 0:
                raise ValueError("Invalid output chunk length")
            actual = raw[offset:offset+chunk["length"]]
            originals = [frames[i][1] for i in indices]
            if len(indices) == 1:
                if actual != originals[0]:
                    raise ValueError("Single event changed")
            else:
                header, first = parse(originals[0])
                delta = first.get("delta", {})
                field = {"text_delta": "text", "thinking_delta": "thinking"}.get(delta.get("type"))
                if first.get("type") != "content_block_delta" or not field or set(delta) != {"type", field}:
                    raise ValueError("Non-text event merged")
                expected = copy.deepcopy(first)
                for original in originals[1:]:
                    other_header, other = parse(original)
                    left, right = copy.deepcopy(first), copy.deepcopy(other)
                    left["delta"][field] = right["delta"][field] = ""
                    if header != other_header or left != right or not isinstance(other["delta"].get(field), str):
                        raise ValueError("Incompatible event merged")
                    expected["delta"][field] += other["delta"][field]
                if parse(actual) != (header, expected):
                    raise ValueError("Combined text or metadata changed")
                if len(actual) > 16384:
                    raise ValueError("Combined event exceeds byte bound")
            targets = [started+frames[i][0]*scale for i in indices]
            delivered = chunk["monotonic_s"]
            if delivered < max(targets)-1e-6:
                raise ValueError("Future event delivered early")
            delays.append(delivered-min(targets))
            # Intentional buffering is window; >250 ms beyond it is invalid.
            if delivered-min(targets) > window+.25:
                raise ValueError("Buffer deadline plus scheduling tolerance exceeded")
            lags.append(max(0, delivered-max(targets)-window))
            offset += chunk["length"]
            cursor += len(indices)
        if cursor != len(frames) or offset != len(raw):
            raise ValueError("Incomplete stream coverage")
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        errors.append(str(exc))
    return dict(valid=not errors, errors=errors, input_events=len(frames), output_events=len(chunks),
                source_to_delivery_max_s=max(delays, default=0), excess_delay_max_s=max(lags, default=0))
