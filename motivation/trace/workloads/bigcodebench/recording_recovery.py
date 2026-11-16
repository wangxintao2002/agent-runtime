import sys
"""Explicit census of a stopped collection; never rewrite its failed status."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time


def read(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def partial_records(collection):
    census = read(collection/'attempt-census.json')
    if read(collection/'status.json')['phase'] != 'stopped':
        raise ValueError('Recovery census requires a stopped collection')
    for path, expected in census['evidence_sha256'].items():
        if digest(Path(path)) != expected:
            raise ValueError('Stopped collection evidence changed')
    rows = [r for p in census['shards'] for r in read(Path(p)/'status.json')['tasks']]
    if rows != census['tasks']:
        raise ValueError('Census does not match original attempt records')
    for run in census['runs']:
        path = Path(run['path'])
        group = Path(read(path/'metadata.json')['cgroup'])
        if group.exists() or group.parent.exists():
            raise ValueError('Attempt cgroup remains')
        if not ((path/'cleanup.json').exists() or (path/'cleanup-recovery.json').exists()):
            raise ValueError('Attempt cleanup evidence missing')
    return rows


def recover(collection):
    collection = collection.resolve()
    state = read(collection/'status.json')
    if state['phase'] != 'stopped':
        raise ValueError('Only stopped collections can be recovered')
    shards = [Path(p) for p in state['collections']]
    rows = [r for p in shards for r in read(p/'status.json')['tasks']]
    ids = [r['task_id'] for r in rows]
    if len(ids) != len(set(ids)) or not set(ids) <= set(read(collection/'plan.json')['task_ids']):
        raise ValueError('Attempt identities inconsistent')
    delegated = Path(f'/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service')
    runs = []
    evidence = [collection/'status.json', collection/'plan.json']
    for shard in shards:
        if read(shard/'status.json')['phase'] not in ('stopped', 'complete'):
            raise ValueError('Shard is not terminal')
        evidence.extend([shard/'status.json', shard/'plan.json'])
    for row in rows:
        for key in ('source', 'qualification'):
            if key not in row:
                continue
            path = Path(row[key]); meta = read(path/'metadata.json')
            if meta['task'] != row['task_id']:
                raise ValueError('Attempt metadata identity mismatch')
            group = Path(meta['cgroup']); root = group.parent
            if root.parent != delegated or not root.name.startswith('bigcode_pilot_') or group.name != 'agent':
                raise ValueError('Unexpected recovery cgroup')
            if root.exists():
                if 'populated 1' in (root/'cgroup.events').read_text():
                    raise RuntimeError('Attempt still has live processes; investigate before recovery')
                events = (root/'memory.events').read_text()
                if group.exists():
                    group.rmdir()
                root.rmdir()
                (path/'cleanup-recovery.json').write_text(json.dumps(dict(
                    cgroup_empty_and_removed=True, observed_populated=False,
                    original_memory_events=events, recovered_s=time.time(),
                    original_outcome_unchanged=True), indent=2)+'\n')
            if not ((path/'cleanup.json').exists() or (path/'cleanup-recovery.json').exists()):
                raise ValueError('Missing original and recovery cleanup evidence')
            evidence.append(path/'metadata.json')
            runs.append(dict(path=str(path), cgroup_removed=True))
    census = dict(shards=list(map(str, shards)), tasks=rows, runs=runs,
                  attempted=len(rows), qualified=sum(bool(r.get('qualified')) for r in rows),
                  original_phase='stopped', original_outcome_unchanged=True,
                  evidence_sha256={str(p): digest(p) for p in evidence}, created_s=time.time())
    target = collection/'attempt-census.json'
    with target.open('x') as file:
        file.write(json.dumps(census, indent=2)+'\n')
    partial_records(collection)
    return census


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('collection', type=Path)
    args = parser.parse_args()
    result = recover(args.collection)
    print(json.dumps({k: result[k] for k in ('attempted', 'qualified', 'original_phase')}))
