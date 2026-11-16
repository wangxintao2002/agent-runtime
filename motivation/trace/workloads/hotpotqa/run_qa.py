"""Real Claude QA pilot with model recording/replay and a real shared retriever.

Separate from the software-repair adapter; does not alter the running corpus job.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
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
RUNS = run_root('hotpotqa')
from runtime import ProcessDetails, Observer, counters, child_environment, MODEL
from record import Recorder, save
from replay import ReplayServer
from audit_replay import audit
from summarize import summarize


def evaluate(data, identity, workspace):
    spec = importlib.util.spec_from_file_location('hotpot_official', data/'official_evaluator.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    gold = next(x for x in json.loads((data/'gold.evaluator-only.json').read_text()) if x['_id'] == identity)
    try:
        answer = json.loads((workspace/'answer.json').read_text())
        assert isinstance(answer['answer'], str)
        assert isinstance(answer['supporting_facts'], list)
        assert all(isinstance(x,list) and len(x)==2 and isinstance(x[0],str) and
                   isinstance(x[1],int) for x in answer['supporting_facts'])
    except (OSError, ValueError, KeyError, TypeError, AssertionError):
        return dict(valid_output=False, answer_em=0., answer_f1=0., supporting_em=0., joint_em=0.)
    metrics = dict(sp_em=0., sp_f1=0., sp_prec=0., sp_recall=0.)
    module.update_sp(metrics, answer['supporting_facts'], gold['supporting_facts'])
    em = float(module.exact_match_score(answer['answer'], gold['answer']))
    return dict(valid_output=True, answer_em=em, answer_f1=module.f1_score(answer['answer'],gold['answer'])[0],
                supporting_em=metrics['sp_em'], joint_em=em*metrics['sp_em'])


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
    p.add_argument('--service', type=Path, required=True)
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
    p.add_argument('--score-answers', action='store_true',
                   help='Optional offline diagnostic only; never a collection eligibility condition')
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
    data, service = args.data.resolve(), args.service.resolve()
    source = args.replay_source.resolve() if args.replay_source else None
    score_answers = args.score_answers
    ready = json.loads((service/'ready.json').read_text())
    dataset_manifest = json.loads((data/'manifest.json').read_text())
    task = next(t for t in json.loads((data/'tasks.json').read_text()) if t['id']==args.task_id)
    if source:
        source_meta = json.loads((source/'metadata.json').read_text())
        # Preserve old replay diagnostics; new recordings do not score by default.
        score_answers = source_meta.get('answer_scoring_enabled',
            json.loads((source/'summary.json').read_text()).get('scores') is not None)
        if source_meta['task'] != args.task_id or not json.loads((source/'summary.json').read_text())['trace_complete']:
            raise ValueError('Incomplete/mismatched source')
        if source_meta['service_manifest_sha256'] != hashlib.sha256((service/'ready.json').read_bytes()).hexdigest():
            raise ValueError('Retriever configuration changed')
    out = RUNS/args.name
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    repo, profile = out/'workspace', out/'profile'
    repo.mkdir(); profile.mkdir(mode=0o700); (out/'tmp').mkdir()
    shutil.copyfile(HERE/'search_client.py', repo/'search.py')
    save(repo/'connection.json', dict(url=ready['url'], task_id=task['id']))
    (repo/'question.txt').write_text(task['question']+'\n')
    initial = snapshot(repo)
    prompt = ('Answer the multi-hop question in question.txt using the local retrieval tool. '
        'Run `python search.py "your search query"` to search the provided Wikipedia passages. '
        'Use this real retrieval tool at least once, and refine queries if needed. Returned sentences have zero-based indices. '
        'Write answer.json with exactly the fields "answer" (short answer string) and "supporting_facts" '
        '(list of [exact title, sentence index] pairs). Briefly explain your evidence in the final response. '
        'Do not modify search.py, connection.json or question.txt. Do not install packages, use external network, '
        'read outside this workspace, inspect environment/credentials, or start background jobs. '
        'The provided local search client is the only permitted network operation.')
    if source:
        # Recorded prompts are part of the replay contract, including older versions.
        prompt=(source/'prompt.txt').read_text()
    else:
        prompt += (' The local corpus is a subset and may not contain the evidence needed. '
            'If repeated query refinements do not find relevant evidence, stop searching and finish '
            'with an honest best-effort answer or "insufficient evidence" and only the supporting '
            'facts actually found. Missing evidence is an acceptable completed outcome; do not '
            'continue searching indefinitely or fabricate supporting facts. '
            'The workspace contains question.txt, search.py and connection.json. Read question.txt '
            'directly with Read and invoke search.py directly with Bash; no directory exploration '
            'or inspection of search.py/connection.json is needed. Do not run ls, stat, date, pwd, '
            'timing commands or commands that print filesystem timestamps. Use Write to save answer.json.')
    (out/'prompt.txt').write_text(prompt)
    # Keep legacy audit's patch fields empty; QA artifacts are checked separately.
    for name in ('candidate.patch','all-changes.patch'):
        (out/name).write_text('')
    delegated = Path(f'/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service')
    if args.cgroup_parent:
        parent=args.cgroup_parent.resolve()
        if not parent.is_relative_to(delegated) or not parent.is_dir():p.error('Invalid delegated batch parent')
        delegated=parent
    root = delegated/('qa_pilot_'+uuid.uuid4().hex[:12])
    root.mkdir()
    group = root/'agent'
    server = observer = proc = None
    unit = 'qa-pilot-'+uuid.uuid4().hex+'.scope'
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
                   PYTHONDONTWRITEBYTECODE='1', QA_RUN_ID=args.name)
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
            workload='HotpotQA real shared retrieval',corpus_scope=ready['corpus_scope'],
            executor='claude',mode='model_replay' if source else 'online',
            model=MODEL,claude_version=subprocess.check_output([binary,'--version'],text=True).strip(),
            service=str(service),service_manifest_sha256=hashlib.sha256((service/'ready.json').read_bytes()).hexdigest(),
            dataset_manifest=dataset_manifest,
            data=str(data),adapter='qa',
            max_model_requests=source_meta.get('max_model_requests',20) if source else args.max_model_requests,
            execution_timeout_s=args.timeout_s,
            prompt_policy='source-preserved' if source else 'subset-evidence-direct-tools-v3',
            answer_scoring_enabled=score_answers,
            collection_policy='normal termination and complete faithful trace; answer correctness is not a criterion',
            response_delay_scale=args.delay_scale if source else None,
            replay_source=str(source) if source else None, gold_visible_to_agent=False,
            cgroup=str(group), memory_max_bytes=4*1024**3,
            scope='agent/tools cgroup; shared retrieval process observed separately, not included in agent RSS',
            source_sha256={str(f.relative_to(HERE.parents[1])):hashlib.sha256(f.read_bytes()).hexdigest()
                           for f in [*HERE.glob('*.py'), *COMMON.glob('*.py')]}))
        observer=Observer(root,{'agent':group},out)
        observer.io=ProcessDetails()
        observer.prepare()
        if args.start_gate:
            save(out/'ready.json',dict(group=str(group),root=str(root),pid=os.getpid()))
            deadline=time.monotonic()+args.gate_timeout_s
            while not args.start_gate.exists():
                if time.monotonic()>deadline:raise TimeoutError('Batch did not release QA start gate')
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
                if time.monotonic()>deadline:raise TimeoutError('Batch did not release QA verification gate')
                time.sleep(.02)
        outcome=evaluate(data,task['id'],repo) if score_answers else None
        artifacts=snapshot(repo); save(out/'workspace-sha256.json',artifacts)
        unchanged=all(artifacts.get(k)==v for k,v in initial.items())
        evidence=[json.loads(line) for line in (service/'requests.jsonl').read_text().splitlines()] if (service/'requests.jsonl').exists() else []
        searches=[r for r in evidence if r.get('run_id')==args.name]
        save(out/'retrieval-evidence.json',searches)
        validation=dict(after=outcome,agent_returncode=proc.returncode,timed_out=timed_out,
            resource_observation_valid=quality['valid'],inputs_unchanged=unchanged,
            real_searches=len(searches),independent_tests_pass=outcome['joint_em']==1. if outcome else None)
        save(out/'validation.json',validation)
        events=[json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
        hooks=[e['payload'] for e in events if e['event']=='hook']
        pre={h['tool_use_id'] for h in hooks if h['hook_event_name']=='PreToolUse'}
        post={h['tool_use_id'] for h in hooks if h['hook_event_name'] in ('PostToolUse','PostToolUseFailure')}
        result_rows=[json.loads(line) for line in (out/'claude.jsonl').read_text().splitlines()]
        finished=any(r.get('type')=='result' and r.get('subtype')=='success' and not r.get('is_error') for r in result_rows)
        complete=bool(proc.returncode==0 and not timed_out and finished and pre==post and pre
                      and unchanged and searches and quality['valid'])
        if source:
            report=server.report()
            report['qa_artifacts_identical']=artifacts==json.loads((source/'workspace-sha256.json').read_text())
            report['acceptance_outcome_identical']=outcome==json.loads((source/'validation.json').read_text())['after']
            report['complete']=bool(report['complete'] and report['qa_artifacts_identical'] and report['acceptance_outcome_identical'])
            save(out/'replay-validation.json',report)
            raw=audit(out); raw['qa_artifacts_identical']=report['qa_artifacts_identical']
            raw['raw_evidence_matches']=bool(raw['raw_evidence_matches'] and report['qa_artifacts_identical'])
            save(out/'replay-audit.json',raw)
            complete=bool(complete and report['complete'] and raw['raw_evidence_matches'])
        common_summary=summarize(out)
        complete=bool(complete and common_summary['trace_complete'])
        summary=dict(common_summary,trace_complete=complete,pilot_valid=complete,task=task['id'],
            benchmark_correct=outcome['joint_em']==1. if outcome else None,scores=outcome,agent_window_s=end-start,
            collection_eligible=complete,answer_scoring_enabled=score_answers,
            tool_calls=len(pre),real_searches=len(searches),model_requests=server.request_count,
            upstream_requests=0 if source else server.request_count,observation_valid=quality['valid'],
            agent_cpu_s=(after['cpu.stat']['usage_usec']-before['cpu.stat']['usage_usec'])/1e6,
            workload_scope=ready['corpus_scope'])
        summary.update(adapter='qa',wall_s=end-start,cpu_s=summary['agent_cpu_s'],
            independent_tests_pass=outcome['joint_em']==1. if outcome else None,
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
            if time.monotonic()>deadline: raise RuntimeError('QA cgroup remains populated')
            time.sleep(.05)
        if group.exists():group.rmdir()
        root.rmdir(); save(out/'cleanup.json',dict(cgroup_empty_and_removed=True))


if __name__=='__main__':
    sys.exit(main())
