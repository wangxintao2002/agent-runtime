import json
from pathlib import Path
import tempfile
import unittest

from analyze_recording import analyze


class RecordingAnalysisTests(unittest.TestCase):
    def test_independent_observer_missing_breakdown_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / 'window.json').write_text(json.dumps(dict(start_s=0, end_s=2)))
            rows = [dict(monotonic_s=t, pool={
                'memory.current': value, 'memory.stat': None, 'pids.current': None})
                for t, value in [(0, 2**20), (1, None), (2, 3 * 2**20)]]
            (p / 'resources.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
            report = analyze(p)
            self.assertEqual(report['peak_mib'], 3)
            self.assertIsNone(report['peak_anon_mib'])
            self.assertIsNone(report['peak_file_mib'])
            self.assertIsNone(report['max_tasks_including_threads'])
            self.assertEqual(report['missing_memory_samples'], 1)
            self.assertEqual(report['memory_sample_coverage_s'], 1)

    def test_time_weighted_memory_is_clipped_to_execution_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "window.json").write_text(json.dumps({"start_s": 1, "end_s": 4}))
            (p / "resources.jsonl").write_text("".join(json.dumps({"monotonic_s": t, "pool": {
                "memory.current": mib * 2**20, "memory.stat": {"anon": mib * 2**20}, "pids.current": 3}}) + "\n"
                for t, mib in [(0, 100), (2, 500), (5, 9999)]))
            a = analyze(p)
            self.assertEqual(a["memory_sample_coverage_s"], 3)
            self.assertEqual(a["memory_mib_seconds"], 1100)
            self.assertEqual(a["time_above_mib_s"]["256"], 2)
            self.assertEqual(a["max_tasks_including_threads"], 3)
            self.assertEqual(a["peak_mib"], 500)

    def test_model_failure_not_confused_with_incomplete_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "summary.json").write_text(json.dumps({"trace_complete": True, "independent_tests_pass": False}))
            self.assertEqual(analyze(p)["status"], "complete_model_failure")
            (p / "summary.json").write_text('{}')
            d = p / "requests/0001"
            d.mkdir(parents=True)
            (d / "request.json").write_text('{}')
            self.assertEqual(analyze(p)["status"], "online_incomplete_needs_review")

    def test_legacy_valid_summary_is_not_treated_as_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "summary.json").write_text(json.dumps({"pilot_valid": True, "independent_tests_pass": True}))
            self.assertEqual(analyze(p)["status"], "complete_correct")


if __name__ == "__main__":
    unittest.main()
