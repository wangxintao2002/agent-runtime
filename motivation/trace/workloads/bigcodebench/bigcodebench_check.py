"""Execute real local BigCodeBench tests and return structured tool feedback.

The reference solution is never executed or returned. This is a workload tool,
not an isolation boundary or the full official evaluator. Test correctness is
feedback to the Agent, not a trace-selection criterion. The host/cgroup runner
must contain the tool and its descendants.
"""
import argparse
import contextlib
import hashlib
import io
import json
import random
import signal
import time
import unittest
from pathlib import Path


class TestDeadline(KeyboardInterrupt):
    """Escape unittest's normal exception capture when the whole tool expires."""


class Feedback(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.issues=[]

    def addError(self,test,err):
        super().addError(test,err)
        self.issues.append(dict(test=test.id(),kind='error',exception=err[0].__name__,message=str(err[1])))

    def addFailure(self,test,err):
        super().addFailure(test,err)
        self.issues.append(dict(test=test.id(),kind='failure',exception=err[0].__name__,message=str(err[1])))


class DiscardOutput(io.TextIOBase):
    """Tool feedback is the test report, not arbitrary test/candidate prints."""
    def write(self,value):
        return len(value)


def evaluate(candidate, test_source, timeout_s=60):
    if not 0<timeout_s<=300:
        raise ValueError('Test timeout must be positive and at most 300 seconds')
    def timeout(*_):
        raise TestDeadline('Local test execution deadline exceeded')
    previous=signal.signal(signal.SIGALRM,timeout)
    signal.setitimer(signal.ITIMER_REAL,timeout_s)
    result=Feedback()
    report=dict(tests_run=0,success=False,failures=0,errors=0,skipped=0,issues=[],timed_out=False,
                evaluator='local unittest execution; no reference solution execution')
    try:
        with contextlib.redirect_stdout(DiscardOutput()),contextlib.redirect_stderr(DiscardOutput()):
            random.seed(0)
            try:
                import numpy as np
                np.random.seed(0)
            except ImportError:
                pass
            namespace={'__name__':'bigcodebench_candidate','__file__':str(candidate)}
            exec(compile(candidate.read_text(),str(candidate),'exec'),namespace)
            exec(compile(test_source,'<bigcodebench-tests>','exec'),namespace)
            case=namespace.get('TestCases')
            if not isinstance(case,type) or not issubclass(case,unittest.TestCase):
                raise ValueError('Official TestCases class is missing')
            unittest.defaultTestLoader.loadTestsFromTestCase(case).run(result)
        report.update(tests_run=result.testsRun,success=result.wasSuccessful() and result.testsRun>0,
            failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),
            issues=result.issues)
    except (Exception,TestDeadline) as exc:
        report.update(tests_run=result.testsRun,errors=len(result.errors)+1,
            failures=len(result.failures),skipped=len(result.skipped),
            issues=[*result.issues,dict(kind='execution',exception=type(exc).__name__,message=str(exc))],
            timed_out=isinstance(exc,TestDeadline))
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        signal.signal(signal.SIGALRM,previous)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reference',required=True,type=Path)
    p.add_argument('--task-id',required=True)
    p.add_argument('--candidate',type=Path,default=Path('solution.py'))
    p.add_argument('--timeout-s',type=float,default=60)
    p.add_argument('--evidence',type=Path,help='Controller-side execution log; never copied into model feedback')
    a=p.parse_args()
    rows=json.loads(a.reference.read_text())
    matches=[r for r in rows if r['id']==a.task_id]
    if len(matches)!=1:raise ValueError('Expected exactly one frozen task')
    # Only the official tests enter execution. canonical_solution is ignored.
    candidate=a.candidate.resolve()
    candidate_hash=hashlib.sha256(candidate.read_bytes()).hexdigest() if candidate.is_file() else None
    started=time.monotonic()
    report=evaluate(candidate,matches[0]['test'],a.timeout_s)
    if a.evidence:
        with a.evidence.open('a') as stream:
            stream.write(json.dumps(dict(task=a.task_id,candidate_sha256=candidate_hash,
                started_s=started,ended_s=time.monotonic(),report=report),sort_keys=True)+'\n')
    print(json.dumps(report,sort_keys=True,ensure_ascii=False))
    return 0 if report['success'] else 1


if __name__=='__main__':
    raise SystemExit(main())
