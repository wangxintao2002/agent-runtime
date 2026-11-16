import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from bigcodebench_check import evaluate


TESTS='''import unittest
class TestCases(unittest.TestCase):
    def test_result(self):
        self.assertEqual(task_func(3), 6)
'''


class ExecutionTests(unittest.TestCase):
    def test_deadline_escapes_unittest_instead_of_continuing_a_hung_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'solution.py';source.write_text('def task_func(x):\n    while True: pass\n')
            report=evaluate(source,TESTS,timeout_s=.2)
            self.assertTrue(report['timed_out']);self.assertFalse(report['success'])

    def test_executes_candidate_and_tests_without_canned_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'solution.py'
            source.write_text('def task_func(x): return x*2\n')
            passed=evaluate(source,TESTS)
            self.assertTrue(passed['success']);self.assertEqual(passed['tests_run'],1)
            source.write_text('def task_func(x): return 0\n')
            failed=evaluate(source,TESTS)
            self.assertFalse(failed['success']);self.assertEqual(failed['failures'],1)
            self.assertEqual(failed['issues'][0]['exception'],'AssertionError')

    def test_candidate_error_and_no_test_class_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'solution.py';source.write_text('raise RuntimeError("candidate failed")\n')
            self.assertEqual(evaluate(source,TESTS)['issues'][0]['exception'],'RuntimeError')
            source.write_text('def task_func(x): return x*2\n')
            report=evaluate(source,'')
            self.assertFalse(report['success']);self.assertEqual(report['tests_run'],0)

    def test_candidate_output_cannot_pollute_structured_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'solution.py';source.write_text('print("arbitrary output")\ndef task_func(x): return x*2\n')
            output=io.StringIO()
            with contextlib.redirect_stdout(output):report=evaluate(source,TESTS)
            self.assertTrue(report['success']);self.assertEqual(output.getvalue(),'')


if __name__=='__main__':unittest.main()
