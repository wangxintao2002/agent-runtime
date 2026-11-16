"""Record a frozen task prefix once and qualify with real-tool model replay."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

HERE=Path(__file__).resolve().parent
COMMON = HERE.parents[1] / 'common'
sys.path.insert(0, str(COMMON))
from paths import DATA, run_root
RUNS = run_root('bigcodebench')
from replay_paths import replay_run_name


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name',required=True)
    p.add_argument('--data',required=True,type=Path)
    p.add_argument('--task-limit',type=int,default=2)
    p.add_argument('--task-start',type=int,default=0)
    a=p.parse_args()
    if not a.name.replace('-','').isalnum():p.error('Invalid collection name')
    data=a.data.resolve();tasks=json.loads((data/'tasks.json').read_text())
    if not 0<=a.task_start<len(tasks) or not 1<=a.task_limit<=len(tasks)-a.task_start:
        p.error('Task selection must fit the frozen distinct corpus')
    tasks=tasks[a.task_start:a.task_start+a.task_limit]
    out=RUNS/a.name;out.mkdir(parents=True, exist_ok=False)
    def save(name,value):
        temp=out/(name+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
        temp.replace(out/(name+'.json'))
    save('plan',dict(data=str(data),task_ids=[t['id'] for t in tasks],task_start=a.task_start,
        task_limit=a.task_limit,selection='frozen ID order; no replacements, retries or correctness selection',
        source_sha256={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in
            (HERE/'run_bigcodebench.py',HERE/'bigcodebench_check.py',HERE/'bigcodebench_client.py')},
        performance_experiment=False,qualification_delay_scale=0,created_s=time.time()))
    records=[];child=None
    def status(phase,**kw):save('status',dict(phase=phase,updated_s=time.time(),tasks=records,**kw))
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,signal.default_int_handler)
    def launch(command,logfile,replay=False):
        nonlocal child
        env=dict(os.environ)
        if replay:env={k:v for k,v in env.items() if not any(x in k.upper() for x in ('API_KEY','TOKEN','SECRET'))}
        with logfile.open('w') as log:
            child=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=log,
                                   stderr=subprocess.STDOUT,start_new_session=True)
            return child.wait()
    try:
        for index,task in enumerate(tasks,start=a.task_start):
            name=f'{a.name}-online-{index:03d}';source=RUNS/name
            row=dict(task_id=task['id'],source=str(source));records.append(row)
            common=[sys.executable,str(HERE/'run_bigcodebench.py'),'--task-id',task['id'],'--data',str(data)]
            status('recording',current_task=task['id'])
            row['online_returncode']=launch([*common,'--name',name],out/f'{index:03d}-online.log')
            if not (source/'cleanup.json').exists():raise RuntimeError('Online task did not publish cleanup evidence')
            if not (source/'summary.json').exists():
                row['error']='online_missing_summary';status('task_incomplete');continue
            summary=json.loads((source/'summary.json').read_text());row['online_trace_complete']=summary['trace_complete']
            if not summary['trace_complete']:
                status('task_incomplete');continue
            replay_name=replay_run_name(f'{a.name}-qual-{index:03d}',source,RUNS)
            qualification=RUNS/replay_name;row['qualification']=str(qualification)
            status('qualifying',current_task=task['id'])
            row['qualification_returncode']=launch([*common,'--name',replay_name,'--replay-source',str(source),
                '--delay-scale','0'],out/f'{index:03d}-qualification.log',replay=True)
            if not (qualification/'cleanup.json').exists():raise RuntimeError('Replay did not publish cleanup evidence')
            f=qualification/'summary.json'
            row['qualified']=bool(f.exists() and json.loads(f.read_text())['trace_complete'])
            status('task_complete')
        save('cleanup',dict(all_task_cleanup_evidence_present=True,stopped=True))
        status('complete',attempted=len(records),qualified=sum(bool(r.get('qualified')) for r in records))
    except BaseException as exc:
        status('stopped',error=type(exc).__name__,detail=str(exc));raise
    finally:
        if child and child.poll() is None:
            os.killpg(child.pid,signal.SIGINT)
            try:child.wait(timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=10)


if __name__=='__main__':main()
