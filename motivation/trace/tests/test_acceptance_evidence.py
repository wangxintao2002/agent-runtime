import unittest
from acceptance_evidence import assess, defect_status


class AcceptanceEvidenceTests(unittest.TestCase):
    def test_body_failure_plus_teardown_error_requires_both_to_be_fixed(self):
        task = {"FAIL_TO_PASS": ["test_a.py::bug"], "PASS_TO_PASS": [], "FAIL_TO_FAIL": []}
        before = "test_a.py::bug FAILED\ntest_a.py::bug ERROR\n"
        after = "test_a.py::bug PASSED\n"
        self.assertTrue(assess(task, before, after, 1, 0)["benchmark_correct"])
        self.assertFalse(assess(task, "test_a.py::bug ERROR\n", after, 1, 0)["baseline_valid"])
        self.assertFalse(assess(task, before, after+"test_a.py::bug ERROR\n", 1, 1)["benchmark_correct"])

    def test_truncated_defect_requires_every_full_node_to_match(self):
        task = {"FAIL_TO_PASS": ["test_a.py::bug[Regression:"], "PASS_TO_PASS": [], "FAIL_TO_FAIL": []}
        before = "test_a.py::bug[Regression: a] FAILED\ntest_a.py::bug[Regression: b] FAILED\n"
        self.assertTrue(all(defect_status(task, before, "FAILED").values()))
        self.assertFalse(all(defect_status(task, before.replace('a] FAILED', 'a] SKIPPED'), "FAILED").values()))
        self.assertTrue(assess(task, before, before.replace('FAILED', 'PASSED'), 1, 0)["benchmark_correct"])
        self.assertFalse(assess(task, before, "test_a.py::bug[Regression: a] PASSED\n", 1, 0)["benchmark_correct"])
    def test_out_of_scope_regressions_prevent_full_benchmark_claim(self):
        task = {"FAIL_TO_PASS": ["test_a.py::bug"], "PASS_TO_PASS": ["test_b.py::old"], "FAIL_TO_FAIL": []}
        a = assess(task, "test_a.py::bug FAILED\n", "test_a.py::bug PASSED\n", 1, 0)
        self.assertTrue(a["observed_scope_correct"])
        self.assertFalse(a["benchmark_correct"])
        self.assertFalse(a["declared_regression_coverage_complete"])

    def test_spaces_in_parameters_expand_all_cases_and_ignore_summary(self):
        task = {"FAIL_TO_PASS": ["test_a.py::bug"], "PASS_TO_PASS": ["test_a.py::old[param"], "FAIL_TO_FAIL": []}
        before = "test_a.py::bug FAILED [33%]\ntest_a.py::old[param a] PASSED [66%]\ntest_a.py::old[param b] PASSED [100%]\nFAILED test_a.py::bug\nFAILED test_a.py::bug\n"
        after = before.replace("bug FAILED", "bug PASSED")
        a = assess(task, before, after, 1, 0)
        self.assertTrue(a["benchmark_correct"])
        self.assertEqual(len(a["dataset_truncated_node_expansions"]["test_a.py::old[param"]), 2)
        self.assertFalse(assess(task, before, after.replace('old[param b] PASSED', 'old[param b] SKIPPED'), 1, 0)["benchmark_correct"])

    def test_known_failure_is_not_new_regression_but_missing_test_is(self):
        task = {"FAIL_TO_PASS": ["test_a.py::bug"], "PASS_TO_PASS": ["test_a.py::old"], "FAIL_TO_FAIL": ["test_a.py::known"]}
        before = "test_a.py::bug FAILED [33%]\ntest_a.py::old PASSED [66%]\ntest_a.py::known FAILED [100%]\n"
        after = before.replace("bug FAILED", "bug PASSED")
        a = assess(task, before, after, 1, 1)
        self.assertTrue(a["benchmark_correct"])
        self.assertFalse(a["module_all_green"])
        for text, rc in [(after.replace('old PASSED', 'old SKIPPED'), 1), (after + 'test_a.py::new FAILED\n', 1), (after, 2)]:
            self.assertFalse(assess(task, before, text, 1, rc)["benchmark_correct"])
        self.assertFalse(assess(task, before.replace('old PASSED', 'old SKIPPED'), after, 1, 1)["baseline_valid"])


if __name__ == "__main__":
    unittest.main()
