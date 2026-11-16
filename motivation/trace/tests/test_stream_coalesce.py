import json
import unittest

from stream_coalesce import TextCoalescer, encode, decode
from audit_coalescing import audit_response


def delta(text, index=0, kind="text_delta"):
    field = {"text_delta": "text", "thinking_delta": "thinking", "input_json_delta": "partial_json"}[kind]
    return encode("event: content_block_delta", dict(type="content_block_delta", index=index, delta={"type": kind, field: text}))


class CoalescingTests(unittest.TestCase):
    def test_merge_preserves_unicode_and_order(self):
        c = TextCoalescer()
        self.assertEqual(c.feed(delta("你好"), 0, 0), [])
        self.assertEqual(c.feed(delta("\n世界"), .005, 1), [])
        output = c.flush()
        self.assertEqual(output[0][1], [0, 1])
        self.assertEqual(decode(output[0][0])[1]["delta"]["text"], "你好\n世界")

    def test_deadline_never_moves_forward_with_more_tokens(self):
        c = TextCoalescer()
        c.feed(delta("a"), 0, 0)
        c.feed(delta("b"), .015, 1)
        self.assertEqual(c.deadline, .02)
        result = c.feed(delta("c"), .021, 2)
        self.assertEqual(result[0][1], [0, 1])
        self.assertEqual(c.flush()[0][1], [2])

    def test_tool_json_signature_and_controls_are_not_merged(self):
        c = TextCoalescer()
        c.feed(delta("a"), 0, 0)
        tool = delta('{"command":', kind="input_json_delta")
        self.assertEqual(c.feed(tool, .001, 1)[-1], (tool, [1]))
        signature = encode("event: content_block_delta", dict(type="content_block_delta", index=0, delta={"type": "signature_delta", "signature": "abc"}))
        self.assertEqual(c.feed(signature, .002, 2), [(signature, [2])])
        self.assertIsNone(c.deadline)

    def test_different_blocks_or_unknown_extensions_flush(self):
        c = TextCoalescer()
        c.feed(delta("a", 0), 0, 0)
        self.assertEqual(c.feed(delta("b", 1), .001, 1)[0][1], [0])
        raw = b'event: x\nid: 1\ndata: {}\n\n'
        self.assertEqual(c.feed(raw, .002, 2)[-1], (raw, [2]))

    def test_memory_is_bounded_even_for_continuous_input(self):
        c = TextCoalescer(max_bytes=150)
        emitted = []
        for i in range(100):
            emitted += c.feed(delta("x" * 20), 0, i)
        emitted += c.flush()
        self.assertEqual([i for _, ids in emitted for i in ids], list(range(100)))
        self.assertTrue(all(len(frame) <= 150 for frame, _ in emitted))

    def test_empty_deltas_cannot_grow_audit_bookkeeping_without_bound(self):
        c = TextCoalescer()
        emitted = []
        for i in range(1000):
            emitted += c.feed(delta(""), 0, i)
        emitted += c.flush()
        self.assertTrue(all(len(indices) <= 256 for _, indices in emitted))
        self.assertEqual(sum(len(indices) for _, indices in emitted), 1000)


class StreamAuditTests(unittest.TestCase):
    def setUp(self):
        self.frames = [(0, delta("hello")), (.005, delta(" world"))]
        self.raw = delta("hello world")
        self.chunks = [dict(monotonic_s=.02, offset=0, length=len(self.raw), source_event_indices=[0, 1])]

    def check(self):
        return audit_response(self.frames, self.raw, self.chunks, 0, 1, .02)

    def test_equivalent_contents_pass_independent_audit(self):
        self.assertTrue(self.check()["valid"])

    def test_lost_text_is_rejected(self):
        self.raw = delta("hello")
        self.chunks[0]["length"] = len(self.raw)
        self.assertFalse(self.check()["valid"])

    def test_reordered_events_are_rejected(self):
        self.chunks[0]["source_event_indices"] = [1, 0]
        self.assertFalse(self.check()["valid"])

    def test_early_and_excessively_late_delivery_rejected(self):
        for timestamp in (0, .4):
            self.chunks[0]["monotonic_s"] = timestamp
            self.assertFalse(self.check()["valid"])

    def test_changed_control_event_rejected(self):
        control = encode("event: message_stop", {"type": "message_stop"})
        self.frames.append((.01, control))
        self.chunks[0]["source_event_indices"].append(2)
        self.assertFalse(self.check()["valid"])
