#!/usr/bin/env python3
"""Entry points for native Claude Code trace collection and qualification."""
import argparse
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent
COMMANDS = {
    'check': 'tools/check_environment.py',
    'swe-export': 'workloads/swe/export_task.py',
    'swe-prepare': 'workloads/swe/prepare_tasks.py',
    'swe-record': 'workloads/swe/run_pilot.py',
    'swe-collect': 'workloads/swe/collect_tasks.py',
    'swe-qualify': 'workloads/swe/qualify_collection.py',
    'swe-analyze': 'workloads/swe/analyze_recording.py',
    'bcb-prepare': 'workloads/bigcodebench/prepare_bigcodebench.py',
    'bcb-record': 'workloads/bigcodebench/run_bigcodebench.py',
    'bcb-collect': 'workloads/bigcodebench/collect_bigcodebench.py',
    'qa-model': 'workloads/hotpotqa/prepare_model.py',
    'qa-prepare': 'workloads/hotpotqa/prepare_wiki_subset.py',
    'qa-service': 'workloads/hotpotqa/retrieval_service.py',
    'qa-record': 'workloads/hotpotqa/run_qa.py',
    'qa-collect': 'workloads/hotpotqa/collect_shared_hotpot.py',
    'audit': 'common/audit_replay.py',
    'summary': 'common/summarize.py',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=COMMANDS)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    script = ROOT / COMMANDS[args.command]
    sys.path[:0] = [str(script.parent), str(ROOT / 'common')]
    sys.argv = [str(script), *args.arguments]
    runpy.run_path(str(script), run_name='__main__')


if __name__ == '__main__':
    main()
