"""Record distinct frozen BigCodeBench tasks in bounded recording shards.

Recording workers are not an Agent admission limit in performance experiments.
Every attempt is retained; correctness never controls eligibility.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from recording_recovery import partial_records

HERE=Path(__file__).resolve().parent
COMMON = HERE.parents[1] / 'common'
sys.path.insert(0, str(COMMON))
from paths import DATA, run_root
RUNS = run_root('bigcodebench')


def partition(start,count,workers):
    if start<0 or count<1 or not 1<=workers<=min(4,count):
        raise ValueError('Invalid recording range/workers')
    sizes=[count//workers+(i<count%workers) for i in range(workers)]
    result=[]
    for size in sizes:
        result.append((start,size));start+=size
    return result


def remaining_ranges(tasks, start, count, attempted):
    """Retain frozen order and never re-record even an interrupted attempt."""
    if len(attempted) != len(set(attempted)):
        raise ValueError('Duplicate previous attempts')
    candidates = tasks[start:start+count]
    if not set(attempted) <= {t['id'] for t in candidates}:
        raise ValueError('Previous attempts outside selected cohort')
    parts = []
    for i in range(start, start+count):
        if tasks[i]['id'] in attempted:
            continue
        if parts and parts[-1][0]+parts[-1][1] == i:
            parts[-1] = (parts[-1][0], parts[-1][1]+1)
        else:
            parts.append((i, 1))
    return parts


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name',required=True)
    p.add_argument('--data',required=True,type=Path)
    p.add_argument('--task-start',type=int,default=0)
    p.add_argument('--task-limit',type=int,required=True)
    p.add_argument('--record-workers',type=int,default=4)
    p.add_argument('--exclude-stopped-collection',type=Path)
    a=p.parse_args()
    if not a.name.replace('-','').isalnum():p.error('Invalid collection name')
    tasks=json.loads((a.data/'tasks.json').read_text())
    if a.task_start+a.task_limit>len(tasks):p.error('Selection outside frozen corpus')
    parts=partition(a.task_start,a.task_limit,a.record_workers)
    previous=[]
    if a.exclude_stopped_collection:
        previous=partial_records(a.exclude_stopped_collection.resolve())
        parts=remaining_ranges(tasks,a.task_start,a.task_limit,[r['task_id'] for r in previous])
        if not parts or len(parts)>a.record_workers:
            p.error('Remaining contiguous ranges must fit recording workers; use an explicit new plan')
    chosen=[t['id'] for start,count in parts for t in tasks[start:start+count]]
    if len(set(chosen))!=len(chosen):p.error('Duplicate frozen task identities')
    out=RUNS/a.name;out.mkdir(parents=True, exist_ok=False)
    def save(name,value):
        temp=out/(name+'.tmp');temp.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')
        temp.replace(out/(name+'.json'))
    save('plan',dict(task_ids=chosen,partitions=parts,record_workers=a.record_workers,
        data=str(a.data.resolve()),correctness_selection=False,performance_experiment=False,
        dataset_manifest_sha256=hashlib.sha256((a.data/'manifest.json').read_bytes()).hexdigest(),
        environment_python=sys.executable,created_s=time.time()))
    files=[Path(__file__),HERE/'recording_recovery.py',HERE/'record_bigcodebench_pilot.py',
           HERE/'run_bigcodebench.py',HERE/'bigcodebench_check.py',HERE/'bigcodebench_client.py',
           COMMON/'process_observe.py',COMMON/'audit_replay.py']
    save('continuation',dict(previous_collection=str(a.exclude_stopped_collection.resolve())
        if a.exclude_stopped_collection else None,excluded_attempt_ids=[r['task_id'] for r in previous],
        no_attempt_retried=True,execution_source_sha256={str(f.resolve()):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}))
    jobs=[];paths=[];logs=[]
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,signal.default_int_handler)
    def status(phase,**kw):save('status',dict(phase=phase,updated_s=time.time(),collections=list(map(str,paths)),**kw))
    try:
        active=subprocess.run(['systemctl','--user','list-units','--plain','--no-legend',
            '--state=active,activating,deactivating','agent-native-*.service'],
            check=True,capture_output=True,text=True)
        if active.stdout.strip():raise RuntimeError('Performance measurement is active')
        for i,(start,count) in enumerate(parts):
            name=f'{a.name}-shard-{i:02d}';paths.append(RUNS/name)
            log=(out/f'shard-{i:02d}.log').open('w');logs.append(log)
            jobs.append(subprocess.Popen([sys.executable,str(HERE/'record_bigcodebench_pilot.py'),
                '--name',name,'--data',str(a.data.resolve()),'--task-start',str(start),'--task-limit',str(count)],
                stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True))
        while any(job.poll() is None for job in jobs):
            states=[json.loads((p/'status.json').read_text()) for p in paths if (p/'status.json').exists()]
            status('recording',tasks_started=sum(len(s['tasks']) for s in states),
                qualified=sum(bool(t.get('qualified')) for s in states for t in s['tasks']))
            if any(job.poll() not in (None,0) for job in jobs):raise RuntimeError('Recording shard failed; retain evidence')
            time.sleep(5)
        if any(job.returncode for job in jobs):raise RuntimeError('Recording shard failed')
        states=[json.loads((p/'status.json').read_text()) for p in paths]
        if not all(s['phase']=='complete' for s in states):raise RuntimeError('Shard incomplete')
        if not all(json.loads((p/'cleanup.json').read_text()).get('stopped') for p in paths):
            raise RuntimeError('Shard cleanup incomplete')
        records=[t for s in states for t in s['tasks']]
        if sorted(t['task_id'] for t in records)!=sorted(chosen):raise RuntimeError('Attempted cohort changed')
        save('cleanup',dict(stopped=True,all_shards_clean=True))
        status('complete',tasks=records,attempted=len(records),qualified=sum(bool(t.get('qualified')) for t in records))
    except BaseException as exc:
        status('stopped',error=type(exc).__name__,detail=str(exc));raise
    finally:
        for job in jobs:
            if job.poll() is None:os.killpg(job.pid,signal.SIGINT)
        for job in jobs:
            if job.poll() is None:
                try:job.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    os.killpg(job.pid,signal.SIGKILL);job.wait(timeout=10)
        for log in logs:log.close()


if __name__=='__main__':main()
