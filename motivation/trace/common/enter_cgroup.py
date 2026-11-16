"""Trusted local launcher: enter a delegated tool cgroup before exec.

Invoked by tool_executor through a temporary user scope. This is accounting
for cooperating local commands, not a security sandbox for hostile programs.
"""
import os
from pathlib import Path
import sys


if __name__ == "__main__":
    group, marker, *command = sys.argv[1:]
    if not command:
        raise SystemExit("Missing command")
    Path(group, "cgroup.procs").write_text(str(os.getpid()))
    temporary = Path(marker + ".tmp")
    temporary.write_text(str(os.getpid()))
    temporary.replace(marker)
    os.execvp(command[0], command)
