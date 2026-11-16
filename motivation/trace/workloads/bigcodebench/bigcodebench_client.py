"""Workspace entry point for the controller's real local test tool."""
import os
import subprocess
import sys


if __name__=='__main__':
    raise SystemExit(subprocess.call([sys.executable,os.environ['BCB_CHECK_PROGRAM'],
        '--reference',os.environ['BCB_REFERENCE'],'--task-id',os.environ['BCB_TASK_ID'],
        '--candidate','solution.py','--timeout-s','60','--evidence',os.environ['BCB_TOOL_EVIDENCE']]))
