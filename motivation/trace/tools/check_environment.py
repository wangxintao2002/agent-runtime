"""Read-only native recording prerequisites. Never print credential values."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cgroup-parent', type=Path)
    args = parser.parse_args()
    uid = os.getuid()
    parent = args.cgroup_parent or Path(f'/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service')
    binaries = {name: shutil.which(name) for name in ('claude', 'git', 'systemd-run', 'systemctl', 'taskset')}
    version = None
    if binaries['claude']:
        version = subprocess.check_output([binaries['claude'], '--version'], text=True, timeout=20).strip()
    controllers = (parent / 'cgroup.controllers').read_text().split() if (parent / 'cgroup.controllers').exists() else []
    enabled = (parent / 'cgroup.subtree_control').read_text().split() if (parent / 'cgroup.subtree_control').exists() else []
    # cpu.stat / CPU PSI are available without enabling CPU bandwidth control.
    # Recording does not allocate CPU quotas; it requires memory/pids limits.
    controls = ('memory', 'pids')
    checks = {
        'linux': sys.platform == 'linux',
        'python_3_11_or_newer': sys.version_info >= (3, 11),
        'binaries_present': all(binaries.values()),
        'cgroup_v2': Path('/sys/fs/cgroup/cgroup.controllers').exists(),
        'parent_writable': os.access(parent, os.W_OK),
        'controllers_available': all(c in controllers for c in controls),
        'controllers_enabled_for_children': all(c in enabled for c in controls),
        'cpu_accounting_readable': os.access(parent / 'cpu.stat', os.R_OK),
        'pressure_readable': all(os.access(parent / f'{r}.pressure', os.R_OK)
                                 for r in ('cpu', 'memory', 'io')),
    }
    print(json.dumps(dict(checks=checks, binaries=binaries, claude_version=version,
        python=sys.version, cgroup_parent=str(parent), controllers=controllers,
        subtree_control=enabled, credential_present=bool(os.environ.get('DS_API_KEY') or os.environ.get('DEEPSEEK_API_KEY')),
        api_request_made=False, cgroups_created=False,
        note='Read-only checks; actual cgroup launch and task dependencies require workload preflight.'), indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
