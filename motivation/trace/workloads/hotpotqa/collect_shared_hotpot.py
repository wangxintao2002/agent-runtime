"""Wait for frozen corpus preparation, record real QA tools, clean shared service."""
import argparse
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time

HERE=Path(__file__).resolve().parent
COMMON = HERE.parents[1] / 'common'
sys.path.insert(0, str(COMMON))
from paths import DATA, run_root
RUNS = run_root('hotpotqa')


def main():
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    p=argparse.ArgumentParser()
    p.add_argument('--name',required=True)
    p.add_argument('--data',required=True,type=Path)
    p.add_argument('--port',type=int,default=18762)
    p.add_argument('--record-workers',type=int,default=1)
    p.add_argument('--task-limit',type=int)
    p.add_argument('--task-start',type=int,default=0)
    a=p.parse_args()
    if not a.name.replace('-','').isalnum():p.error('Invalid name')
    if not 1<=a.record_workers<=8:p.error('Recording workers must be 1..8')
    if a.task_limit is not None and a.task_limit<1:p.error('Task limit must be positive')
    if a.task_start<0:p.error('Task start must be nonnegative')
    out=RUNS/a.name;out.mkdir(parents=True, exist_ok=False)
    def status(phase,**extra):
        q=out/'status.tmp';q.write_text(json.dumps(dict(phase=phase,updated_s=time.time(),**extra),indent=2)+'\n')
        q.replace(out/'status.json')
    status('waiting_for_corpus',data=str(a.data.resolve()))
    while True:
        path=a.data/'status.json'
        if path.exists():
            state=json.loads(path.read_text())
            if state['phase']=='complete':break
            if state['phase'].startswith(('failed','cancelled','stopped')):
                status('stopped_preparation');raise RuntimeError('Preparation stopped')
        time.sleep(10)
    unit='qa-collection-'+a.name+'.service'
    service=RUNS/(a.name+'-service')
    status('starting_service',unit=unit,service=str(service))
    command=['systemd-run','--user','--quiet','--collect','--unit='+unit,
        '--property=MemoryMax=16G','--property=MemorySwapMax=0',
        '--property=TasksMax=1024','--property=RuntimeMaxSec=14400',
        '--setenv=PATH='+os.environ['PATH'],'--setenv=OPENBLAS_NUM_THREADS=1',
        '--setenv=OMP_NUM_THREADS=2',sys.executable,str(HERE/'retrieval_service.py'),
        '--data',str(a.data.resolve()),'--out',str(service),'--shared-index','--port',str(a.port)]
    jobs=[];logs=[]
    try:
        subprocess.run(command,check=True,capture_output=True)
        deadline=time.monotonic()+180
        while not (service/'ready.json').exists():
            if time.monotonic()>deadline:raise TimeoutError('Retriever did not become ready')
            state=subprocess.run(['systemctl','--user','is-active',unit],capture_output=True,text=True)
            if state.stdout.strip() not in ('active','activating'):raise RuntimeError('Retriever exited before readiness')
            time.sleep(1)
        collections=[]
        status('recording',service=str(service),recording_workers=a.record_workers)
        for shard in range(a.record_workers):
            name=a.name+'-tasks'+(f'-{shard:02d}' if a.record_workers>1 else '')
            collections.append(RUNS/name)
            log=(out/f'collection-{shard:02d}.log').open('w');logs.append(log)
            jobs.append(subprocess.Popen([sys.executable,str(HERE/'record_hotpot_pilot.py'),
                '--name',name,'--data',str(a.data.resolve()),'--service',str(service),
                '--shard-index',str(shard),'--shard-count',str(a.record_workers),
                '--task-start',str(a.task_start),
                *(['--task-limit',str(a.task_limit)] if a.task_limit is not None else [])],
                stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True))
        while any(job.poll() is None for job in jobs):
            progress=[json.loads((path/'status.json').read_text()) for path in collections if (path/'status.json').exists()]
            status('recording',service=str(service),recording_workers=a.record_workers,
                tasks_started=sum(len(x.get('tasks',[])) for x in progress),
                qualified=sum(bool(t.get('qualified')) for x in progress for t in x.get('tasks',[])),
                collections=list(map(str,collections)))
            time.sleep(5)
        if any(job.returncode for job in jobs):raise RuntimeError('Collection driver failed')
        results=[json.loads((path/'status.json').read_text()) for path in collections]
        records=[t for r in results for t in r['tasks']]
        if len({t['task_id'] for t in records})!=len(records):raise RuntimeError('Duplicate recording task')
        status('complete',attempted=len(records),qualified=sum(bool(t.get('qualified')) for t in records),
               collections=list(map(str,collections)),tasks=records)
    except BaseException as exc:
        status('stopped_error',error=type(exc).__name__)
        raise
    finally:
        for job in jobs:
            if job.poll() is None:os.killpg(job.pid,signal.SIGINT)
        for job in jobs:
            if job.poll() is None:
                try:job.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(job.pid,signal.SIGKILL);job.wait(timeout=10)
        for log in logs:log.close()
        subprocess.run(['systemctl','--user','stop',unit],capture_output=True)
        state=subprocess.run(['systemctl','--user','is-active',unit],capture_output=True,text=True)
        (out/'cleanup.json').write_text(json.dumps(dict(unit=unit,state=state.stdout.strip(),
            stopped=state.stdout.strip() not in ('active','activating','deactivating')))+'\n')


if __name__=='__main__':main()
