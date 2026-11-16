#!/usr/bin/env python3
"""Offline regression suite; no Claude process, API credential or model request."""
import argparse
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'common'), *(str(ROOT / 'workloads' / w)
                for w in ('swe', 'bigcodebench', 'hotpotqa'))]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern', default='test_*.py')
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=args.pattern)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
