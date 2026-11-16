"""Real Claude Code on frozen BigCodeBench tasks, with actual local tests.

Online recording and strict model-only replay; answer correctness is unscored.
No per-task Docker, reference solution execution, or precomputed tool replies.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
COMMON = HERE.parents[1] / 'common'
sys.path.insert(0, str(COMMON))
from paths import DATA, run_root
RUNS = run_root('bigcodebench')
from runtime import ProcessDetails, Observer, counters, child_environment, MODEL
from record import Recorder, save
from replay import ReplayServer
from audit_replay import audit
from summarize import summarize


def environment_manifest(data):
    packages=sorted([d.metadata['Name'],d.version] for d in importlib.metadata.distributions())
    files={str(f.resolve()):hashlib.sha256(f.read_bytes()).hexdigest() for f in
           (data/'manifest.json',data/'tasks.json',data/'controller-only/reference.json',
            HERE/'bigcodebench_check.py',HERE/'bigcodebench_client.py',Path(__file__))}
    return dict(python=sys.version,executable=sys.executable,packages=packages,files=files,
                random_seed=0,python_hash_seed=0,tool_timeout_s=60)


def snapshot(workspace):
    return {str(p.relative_to(workspace)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in workspace.rglob('*') if p.is_file()}


def main():
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    p = argparse.ArgumentParser()
    p.add_argument('--name', required=True)
    p.add_argument('--task-id', required=True)
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--replay-source', type=Path)
    p.add_argument('--delay-scale', type=float, default=1)
    p.add_argument('--timeout-s', type=float, default=300)
    p.add_argument('--max-model-requests',type=int,default=80,
                   help='Recording failure guard, not a concurrency or resource admission rule')
    p.add_argument('--cgroup-parent',type=Path)
    p.add_argument('--direct-launch',action='store_true')
    p.add_argument('--start-gate',type=Path)
    p.add_argument('--acceptance-gate',type=Path)
    p.add_argument('--gate-timeout-s',type=float,default=120)
    args = p.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.name) or args.timeout_s <= 0 or args.delay_scale < 0 or args.max_model_requests<1:
        p.error('Invalid run name/time budget')
    if not 0<args.gate_timeout_s<=86400 or (args.direct_launch and not args.cgroup_parent):
        p.error('Invalid gate timeout/direct launch parent')
    key = os.environ.get('DEEPSEEK_API_KEY') or os.environ.get('DS_API_KEY')
    if not args.replay_source and not key:
        p.error('Load key from authorized shell; do not print it')
    binary = shutil.which('claude')
    if not binary:
        p.error('Claude not found')
    data = args.data.resolve()
    source = args.replay_source.resolve() if args.replay_source else None
    environment=environment_manifest(data)
    dataset_manifest = json.loads((data/'manifest.json').read_text())
    task = next(t for t in json.loads((data/'tasks.json').read_text()) if t['id']==args.task_id)
    if source:
        source_meta = json.loads((source/'metadata.json').read_text())
        if source_meta['task'] != args.task_id or not json.loads((source/'summary.json').read_text())['trace_complete']:
            raise ValueError('Incomplete/mismatched source')
        if source_meta['environment']!=environment:
            raise ValueError('Frozen BigCodeBench tool/data/Python environment changed')
    out = RUNS/args.name
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    repo, profile = out/'workspace', out/'profile'
    repo.mkdir(); profile.mkdir(mode=0o700); (out/'tmp').mkdir()
    shutil.copyfile(HERE/'bigcodebench_client.py',repo/'check.py')
    (repo/'task.txt').write_text(task['prompt']+'\n')
    initial=snapshot(repo)
    prompt=('Read task.txt and implement its requested Python function in solution.py. '
        'Use the provided local test tool by running `python check.py` at least once. '
        'It executes real tests against your current solution. You may revise your implementation '
        'based on its feedback and run it again. End with a brief explanation of your solution '
        'and any remaining problems; a best-effort solution with failing tests is an acceptable '
        'completed task. Do not keep retrying indefinitely. '
        'The workspace initially contains task.txt and check.py. Read task.txt directly with Read; '
        'use Write for solution.py and Bash for local execution. Do not modify task.txt or check.py, '
        'install dependencies, use the network, read outside this workspace, inspect environment '
        'or credentials, or launch background jobs. The supplied check.py wrapper is the only '
        'permitted way to invoke the controller test tool. Do not inspect its implementation. '
        'Do not run directory listings, date, stat, pwd or timing commands. '
        'For any optional self-written randomized checks, use a fixed seed and print only '
        'deterministic outputs; do not print durations, timestamps or object memory addresses.')
    if source:prompt=(source/'prompt.txt').read_text()
    (out/'prompt.txt').write_text(prompt)
    # Workspace artifacts are checked independently; no git repository is used.
    for name in ('candidate.patch','all-changes.patch'):
        (out/name).write_text('')
    delegated = Path(f'/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service')
    if args.cgroup_parent:
        parent=args.cgroup_parent.resolve()
        if not parent.is_relative_to(delegated) or not parent.is_dir():p.error('Invalid delegated batch parent')
        delegated=parent
    root = delegated/('bigcode_pilot_'+uuid.uuid4().hex[:12])
    root.mkdir()
    group = root/'agent'
    server = observer = proc = None
    unit = 'bigcode-pilot-'+uuid.uuid4().hex+'.scope'
    try:
        for name,value in {'memory.max':4*1024**3,'memory.swap.max':0,'pids.max':512,
                           'cgroup.subtree_control':'+memory +pids'}.items():
            (root/name).write_text(str(value))
        group.mkdir()
        server = ReplayServer(out,source,repo,args.delay_scale) if source else Recorder(out,key,args.max_model_requests)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        save(out/'settings.json', {'hooks':{event:[{'hooks':[{'type':'http','url':server.url+'/hooks',
            'headers':{'Authorization':'Bearer '+server.token},'timeout':5}]}]
            for event in ('SessionStart','SessionEnd','PreToolUse','PostToolUse','PostToolUseFailure','Stop')}})
        base = child_environment(server.token)
        keep={'HOME','USER','LOGNAME','LANG','LC_ALL','TERM','PATH','XDG_RUNTIME_DIR',
              'DBUS_SESSION_BUS_ADDRESS','SSL_CERT_FILE','SSL_CERT_DIR'}
        env={k:v for k,v in base.items() if k in keep or k.startswith(('ANTHROPIC_','CLAUDE_CODE_'))}
        env.update(ANTHROPIC_BASE_URL=server.url+'/anthropic', CLAUDE_CONFIG_DIR=str(profile),
                   LANG='C.UTF-8',LC_ALL='C.UTF-8', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
                   PATH=str(Path(sys.executable).parent)+':'+env['PATH'], TMPDIR=str(out/'tmp'),
                   PYTHONDONTWRITEBYTECODE='1',PYTHONHASHSEED='0',
                   BCB_CHECK_PROGRAM=str(HERE/'bigcodebench_check.py'),
                   BCB_REFERENCE=str(data/'controller-only/reference.json'),BCB_TASK_ID=task['id'],
                   BCB_TOOL_EVIDENCE=str(out/'test-tool-evidence.jsonl'))
        command=[binary,'--setting-sources','','--settings',str(out/'settings.json'),
            '--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--model',MODEL,
            '--no-chrome','--disable-slash-commands','--permission-mode','dontAsk',
            '--tools','Read,Write,Bash','--allowedTools','Read,Write,Bash',
            '--output-format','stream-json','--verbose','--include-partial-messages','-p',prompt]
        entry=COMMON/'enter_cgroup.py'
        launch=['systemd-run','--user','--scope','--quiet','--collect','--unit='+unit,
                sys.executable,str(entry),str(group),str(out/'entered'),*command]
        if args.direct_launch:launch=[sys.executable,str(entry),str(group),str(out/'entered'),*command]
        save(out/'metadata.json',dict(date_utc=datetime.now(timezone.utc).isoformat(), task=task['id'],
            workload='BigCodeBench Instruct real local code and unittest execution',
            executor='claude',mode='model_replay' if source else 'online',
            model=MODEL,claude_version=subprocess.check_output([binary,'--version'],text=True).strip(),
            environment=environment,
            dataset_manifest=dataset_manifest,
            data=str(data),adapter='bigcodebench',
            max_model_requests=source_meta.get('max_model_requests',20) if source else args.max_model_requests,
            execution_timeout_s=args.timeout_s,
            prompt_policy='source-preserved' if source else 'bigcode-local-tests-v1',
            answer_scoring_enabled=False,
            collection_policy='normal termination and complete faithful trace; answer correctness is not a criterion',
            response_delay_scale=args.delay_scale if source else None,
            replay_source=str(source) if source else None, gold_visible_to_agent=False,
            cgroup=str(group), memory_max_bytes=4*1024**3,
            scope='agent and all local test descendants; no remote test service',
            source_sha256={str(f.relative_to(HERE.parents[1])):hashlib.sha256(f.read_bytes()).hexdigest()
                           for f in [*HERE.glob('*.py'), *COMMON.glob('*.py')]}))
        observer=Observer(root,{'agent':group},out)
        observer.io=ProcessDetails()
        observer.prepare()
        if args.start_gate:
            save(out/'ready.json',dict(group=str(group),root=str(root),pid=os.getpid()))
            deadline=time.monotonic()+args.gate_timeout_s
            while not args.start_gate.exists():
                if time.monotonic()>deadline:raise TimeoutError('Batch did not release BigCodeBench start gate')
                time.sleep(.01)
        observer.start()
        before=counters(root); start=time.monotonic(); server.event('agent_launch')
        timed_out=False
        with (out/'claude.jsonl').open('w') as stdout,(out/'stderr.log').open('w') as stderr:
            proc=subprocess.Popen(launch,cwd=repo,env=env,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr)
            while proc.poll() is None or 'populated 1' in (group/'cgroup.events').read_text():
                if time.monotonic()-start>args.timeout_s or (source and server.failed.is_set()):
                    timed_out=time.monotonic()-start>args.timeout_s
                    (root/'cgroup.kill').write_text('1')
                    subprocess.run(['systemctl','--user','stop',unit],capture_output=True)
                    break
                time.sleep(.05)
            proc.wait(timeout=10)
        end=time.monotonic(); after=counters(root)
        server.event('agent_exit',returncode=proc.returncode,timed_out=timed_out)
        quality=observer.stop(); observer=None
        save(out/'window.json',dict(start_s=start,end_s=end,before=before,after=after,
                                    returncode=proc.returncode,timed_out=timed_out,
                                    agent_pid=int((out/'entered').read_text())))
        if args.acceptance_gate:
            deadline=time.monotonic()+max(660,args.gate_timeout_s)
            while not args.acceptance_gate.exists():
                if time.monotonic()>deadline:raise TimeoutError('Batch did not release BigCodeBench verification gate')
                time.sleep(.02)
        outcome=None  # Final correctness never controls collection eligibility.
        artifacts=snapshot(repo); save(out/'workspace-sha256.json',artifacts)
        unchanged=all(artifacts.get(k)==v for k,v in initial.items())
        evidence_path=out/'test-tool-evidence.jsonl'
        tests=[json.loads(line) for line in evidence_path.read_text().splitlines()] if evidence_path.exists() else []
        tests_ok=bool(tests and all(r['task']==task['id'] and r['ended_s']>=r['started_s'] for r in tests))
        validation=dict(after=outcome,agent_returncode=proc.returncode,timed_out=timed_out,
            resource_observation_valid=quality['valid'],inputs_unchanged=unchanged,
            real_test_tool_calls=len(tests),independent_tests_pass=None)
        save(out/'validation.json',validation)
        events=[json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
        hooks=[e['payload'] for e in events if e['event']=='hook']
        pre={h['tool_use_id'] for h in hooks if h['hook_event_name']=='PreToolUse'}
        post={h['tool_use_id'] for h in hooks if h['hook_event_name'] in ('PostToolUse','PostToolUseFailure')}
        result_rows=[json.loads(line) for line in (out/'claude.jsonl').read_text().splitlines()]
        finished=any(r.get('type')=='result' and r.get('subtype')=='success' and not r.get('is_error') for r in result_rows)
        complete=bool(proc.returncode==0 and not timed_out and finished and pre==post and pre
                      and unchanged and tests_ok and quality['valid'])
        if source:
            report=server.report()
            report['workspace_artifacts_identical']=artifacts==json.loads((source/'workspace-sha256.json').read_text())
            report['acceptance_outcome_identical']=outcome==json.loads((source/'validation.json').read_text())['after']
            previous_tests=[json.loads(line) for line in (source/'test-tool-evidence.jsonl').read_text().splitlines()]
            project=lambda rows:[{k:r[k] for k in ('task','candidate_sha256','report')} for r in rows]
            report['real_test_evidence_identical']=project(tests)==project(previous_tests)
            report['complete']=bool(report['complete'] and report['workspace_artifacts_identical']
                and report['acceptance_outcome_identical'] and report['real_test_evidence_identical'])
            save(out/'replay-validation.json',report)
            raw=audit(out); raw['workspace_artifacts_identical']=report['workspace_artifacts_identical']
            raw['real_test_evidence_identical']=report['real_test_evidence_identical']
            raw['raw_evidence_matches']=bool(raw['raw_evidence_matches'] and report['workspace_artifacts_identical']
                and report['real_test_evidence_identical'])
            save(out/'replay-audit.json',raw)
            complete=bool(complete and report['complete'] and raw['raw_evidence_matches'])
        common_summary=summarize(out)
        complete=bool(complete and common_summary['trace_complete'])
        summary=dict(common_summary,trace_complete=complete,pilot_valid=complete,task=task['id'],
            benchmark_correct=None,scores=None,agent_window_s=end-start,
            collection_eligible=complete,answer_scoring_enabled=False,
            tool_calls=len(pre),real_test_tool_calls=len(tests),model_requests=server.request_count,
            upstream_requests=0 if source else server.request_count,observation_valid=quality['valid'],
            agent_cpu_s=(after['cpu.stat']['usage_usec']-before['cpu.stat']['usage_usec'])/1e6,
            workload_scope='frozen BigCodeBench Instruct; real local tests; cooperative host execution')
        summary.update(adapter='bigcodebench',wall_s=end-start,cpu_s=summary['agent_cpu_s'],
            independent_tests_pass=None,
            model_request_count=server.request_count,
            tool_counts=dict(Counter(h['tool_name'] for h in hooks if h['hook_event_name']=='PreToolUse')),
            resource_observation_valid=quality['valid'],sample_gap_max_s=quality['gap_max_s'])
        save(out/'summary.json',summary); print(json.dumps(summary),flush=True)
        return 0 if complete else 2
    finally:
        if proc:
            subprocess.run(['systemctl','--user','stop',unit],capture_output=True)
        (root/'cgroup.kill').write_text('1')
        if proc: proc.wait(timeout=10)
        if observer: observer.stop()
        if server: server.shutdown(); server.server_close()
        deadline=time.monotonic()+10
        while 'populated 1' in (root/'cgroup.events').read_text():
            if time.monotonic()>deadline: raise RuntimeError('BigCodeBench cgroup remains populated')
            time.sleep(.05)
        if group.exists():group.rmdir()
        root.rmdir(); save(out/'cleanup.json',dict(cgroup_empty_and_removed=True))


if __name__=='__main__':
    sys.exit(main())
