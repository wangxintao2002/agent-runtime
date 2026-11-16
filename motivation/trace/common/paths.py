"""Portable locations; generated artifacts never enter source directories."""
import os
from pathlib import Path

TRACE_ROOT = Path(__file__).resolve().parent.parent
COMMON = TRACE_ROOT / 'common'
DATA = Path(os.environ.get('AGENTOS_TRACE_DATA', str(TRACE_ROOT / 'data'))).expanduser().resolve()
RUNS = Path(os.environ.get('AGENTOS_TRACE_RUNS', str(TRACE_ROOT / 'runs'))).expanduser().resolve()

def run_root(workload):
    return RUNS / workload
