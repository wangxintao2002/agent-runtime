"""Record and qualify every frozen QA task, preserving all attempts."""
import argparse
import json
import signal
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
COMMON = HERE.parents[1] / 'common'
sys.path.insert(0, str(COMMON))
from paths import DATA, run_root
RUNS = run_root('hotpotqa')
from replay_paths import replay_run_name


def main():
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    p=argparse.ArgumentParser()
    p.add_argument('--name',required=True)
    p.add_argument('--service',type=Path,required=True)
    p.add_argument('--data',type=Path,required=True)
    p.add_argument('--shard-index',type=int,default=0)
    p.add_argument('--shard-count',type=int,default=1)
    p.add_argument('--task-limit',type=int)
    p.add_argument('--task-start',type=int,default=0,help='Frozen source index; extend collection without repeating earlier questions')
    args=p.parse_args()
    if not args.name.replace('-','').isalnum():p.error('Invalid collection name')
    if not 0<=args.shard_index<args.shard_count<=8:p.error('Invalid bounded recording shard')
    out=RUNS/args.name
    out.mkdir(parents=True, exist_ok=False)
    data=args.data.resolve()
    tasks=json.loads((data/'tasks.json').read_text())
    if not 0<=args.task_start<len(tasks):p.error('Task start outside frozen question set')
    tasks=tasks[args.task_start:]
    if args.task_limit is not None:
        if args.task_limit<1:p.error('Task limit must be positive')
        tasks=tasks[:args.task_limit]
    records=[]
    def save(phase,**extra):
        temporary=out/'status.tmp'
        temporary.write_text(json.dumps(dict(phase=phase,updated_s=time.time(),tasks=records,**extra),indent=2)+'\n')
        temporary.replace(out/'status.json')
    for i,task in enumerate(tasks,start=args.task_start):
        if i%args.shard_count!=args.shard_index:continue
        name=f'{args.name}-online-{i:02d}'
        source=RUNS/name
        row={'task_id':task['id'],'source':str(source)};records.append(row)
        save('online',task_id=task['id'])
        common=[sys.executable,str(HERE/'run_qa.py'),'--task-id',task['id'],'--data',str(data),'--service',str(args.service.resolve())]
        with (out/f'{i:02d}-online.log').open('w') as log:
            result=subprocess.run([*common,'--name',name],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
        row['online_returncode']=result.returncode
        summary=source/'summary.json'
        if not summary.exists():
            row['error']='online_missing_summary';save('online_failed');continue
        row['online_summary']=json.loads(summary.read_text())
        if not row['online_summary']['trace_complete']:
            save('online_incomplete');continue
        replay_name=replay_run_name(f'{args.name}-qual-{i:02d}',source,RUNS)
        replay=RUNS/replay_name
        row['qualification']=str(replay);save('qualification',task_id=task['id'])
        with (out/f'{i:02d}-qualification.log').open('w') as log:
            result=subprocess.run([*common,'--name',replay_name,'--replay-source',str(source),'--delay-scale','0'],
                                  stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
        row['qualification_returncode']=result.returncode
        summary=replay/'summary.json'
        row['qualified']=bool(summary.exists() and json.loads(summary.read_text())['trace_complete'])
        save('task_complete')
    save('complete',qualified=sum(bool(r.get('qualified')) for r in records),attempted=len(records))
    print((out/'status.json').read_text(),flush=True)


if __name__=='__main__':
    main()
