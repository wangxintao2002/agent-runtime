"""Metric-specific evidence contract; never rewrites legacy full validity.

Primary experiment metrics require raw replay fidelity, coherent lifecycle
timestamps, unchanged 50 ms critical sampling limits for EVERY task and the
whole experiment, valid endpoint counters, containment, and clean termination.
Auxiliary 200 ms process snapshots have their original independent limits.
An auxiliary failure is disclosed and excludes process-snapshot claims, not
otherwise valid cgroup counters or lifecycle timestamps. Old failed experiments
are not retroactively promoted into confirmatory comparisons.
"""
import argparse
import json
import math
from pathlib import Path

CONTRACT = 'primary-cgroup-and-lifecycle-v1'
BATCH_CONTRACT = 'lifecycle-and-whole-cgroup-v2'


def read(path):
    return json.loads(path.read_text())


def critical_quality(q):
    required = ('sample_count','requested_interval_s','gap_p95_s','gap_max_s',
                'sampler_rows_enqueued','writer_rows_written','errors','writer_errors')
    if any(k not in q for k in required):
        return False
    return bool(q['requested_interval_s']==.05 and q['sample_count']>3
        and q['gap_p95_s']<=.1 and q['gap_max_s']<=.25
        and q['sampler_rows_enqueued']==q['writer_rows_written']==q['sample_count']
        and not q['errors'] and not q['writer_errors'])


def diagnostic_quality(q):
    required=('diagnostic_samples','diagnostic_gap_p95_s','diagnostic_gap_max_s','diagnostic_errors')
    if any(k not in q for k in required):
        return False
    return bool(q['diagnostic_samples']>2 and q['diagnostic_gap_p95_s']<=.4
        and q['diagnostic_gap_max_s']<=1. and not q['diagnostic_errors']
        and not q.get('process_io_errors'))


def endpoint_counters(window):
    try:
        return bool(math.isfinite(window['start_s']) and math.isfinite(window['end_s'])
            and window['end_s']>window['start_s']
            and 0<=window['before']['cpu.stat']['usage_usec']<=window['after']['cpu.stat']['usage_usec']
            and all(window['after']['memory.events'][k]==window['before']['memory.events'][k]
                    for k in ('max','oom','oom_kill')))
    except (KeyError,TypeError,ValueError):
        return False


def task_contract(summary, validation, audit, window, metric, batch_window, quality, adapter):
    flags=('model_records_ok','tool_hooks_complete','recorded_prompt_matches_intended',
           'task_environment_valid','response_chunk_records_complete','agent_result_ok')
    replay=summary.get('replay_validation') or {}
    fidelity=bool(all(summary.get(k) is True for k in flags)
        and replay.get('complete') is True and replay.get('upstream_requests')==0
        and audit.get('raw_evidence_matches') is True
        and validation.get('agent_returncode')==0 and validation.get('timed_out') is False
        and window.get('returncode')==0 and window.get('timed_out') is False)
    if adapter=='qa':
        fidelity=bool(fidelity and validation.get('inputs_unchanged') is True
                      and replay.get('qa_artifacts_identical') is True
                      and audit.get('qa_artifacts_identical') is True)
    elif adapter=='bigcodebench':
        fidelity=bool(fidelity and validation.get('inputs_unchanged') is True
            and replay.get('workspace_artifacts_identical') is True
            and replay.get('real_test_evidence_identical') is True
            and audit.get('bigcode_artifact_evidence_matches') is True)
    elif adapter!='code':
        fidelity=False
    try:
        timing=bool(all(math.isfinite(metric[k]) for k in ('arrival_s','released_s','start_s','end_s','response_s','execution_s'))
            and batch_window['start_s']<=metric['arrival_s']<=metric['released_s']<=metric['start_s']
            and metric['start_s']<metric['end_s']<=batch_window['end_s']
            and abs(metric['start_s']-window['start_s'])<1e-6
            and abs(metric['end_s']-window['end_s'])<1e-6
            and abs(metric['response_s']-(window['end_s']-metric['arrival_s']))<1e-6
            and abs(metric['execution_s']-(window['end_s']-window['start_s']))<1e-6
            and metric.get('agent_returncode')==0 and metric.get('timed_out') is False)
    except (KeyError,TypeError,ValueError):
        timing=False
    return dict(fidelity=fidelity,timing=timing,endpoint_counters=endpoint_counters(window),
        critical_resource_sampling=critical_quality(quality),
        auxiliary_diagnostics=diagnostic_quality(quality),
        legacy_full_observation_valid=quality.get('valid',False),
        critical_gap_max_s=quality.get('gap_max_s'), diagnostic_gap_max_s=quality.get('diagnostic_gap_max_s'))


def apply_contract(report, contract=CONTRACT):
    """Declare which independent measurements support the reported claims.

    v2 is prospective: per-task sampling retains its existing limits but is not
    a dependency of lifecycle timestamps or whole-experiment resource counters.
    Never relabel the legacy validity or reuse old stopped studies as v2 data.
    """
    if contract not in (CONTRACT, BATCH_CONTRACT):
        raise ValueError('Unknown measurement contract')
    report['contract']=contract
    checks=report.get('checks',{})
    required={'all_raw_audits','exact_distinct_cohort','lifecycle','task_endpoints',
              'experiment_endpoints','experiment_critical_sampling','containment',
              'cleanup','no_guardrail_hit','no_execution_exception','no_upstream_requests'}
    if contract==CONTRACT:required.add('task_critical_sampling')
    report['required_checks']=sorted(required)
    report['primary_valid']=not report.get('errors') and all(checks.get(k) is True for k in required)
    report['task_resource_exclusions']=[dict(task=t['task'],run=t['run'],
        reason='task critical resource samples incomplete; exclude phase resource attribution and sampled task peaks')
        for t in report.get('tasks',[]) if not t.get('critical_resource_sampling')]
    report['metric_scope']=('task lifecycle/tool intervals and whole-experiment resources; task sampled resources separately qualified'
        if contract==BATCH_CONTRACT else 'task lifecycle plus every task and whole-experiment critical resources')
    return report


def assess(run, contract=CONTRACT):
    run=run.resolve()
    result=read(run/'result.json')
    report=dict(contract=CONTRACT,run=str(run),legacy_full_valid=result.get('valid',False),
                primary_valid=False,errors=[],tasks=[])
    try:
        s=result['summary'];plan=read(run/'plan.json');batch=Path(plan['batch'])
        window=read(batch/'window.json');experiment=read(batch/'experiment-window.json')
        quality=read(batch/'observation-quality.json')
        metrics={m['name']:m for m in s['service_metrics']['tasks']}
        identities=[]
        for path in s['per_task_summary_paths']:
            d=Path(path).parent;meta=read(d/'metadata.json');identities.append(meta['task'])
            task=task_contract(read(d/'summary.json'),read(d/'validation.json'),read(d/'replay-audit.json'),
                read(d/'window.json'),metrics[d.name],window,read(d/'observation-quality.json'),meta.get('adapter','code'))
            report['tasks'].append(dict(task=meta['task'],run=str(d),**task))
        containment=read(run/'containment-audit.json');cleanup=read(run/'cleanup.json')
        recovery=read(run/'supervisor-recovery.json')
        guard_lines=(run/'guard.jsonl').read_text().splitlines()
        guard= json.loads(guard_lines[-1]) if guard_lines else {}
        report['checks']=dict(
            all_raw_audits=result.get('raw_audits_passed') is True,
            exact_distinct_cohort=len(identities)==len(set(identities))==len(metrics)==len(plan['task_identities'])
                and set(identities)==set(plan['task_identities']),
            lifecycle=all(t['fidelity'] and t['timing'] for t in report['tasks']) and bool(report['tasks']),
            task_endpoints=all(t['endpoint_counters'] for t in report['tasks']),
            task_critical_sampling=all(t['critical_resource_sampling'] for t in report['tasks']),
            experiment_endpoints=endpoint_counters(experiment) and endpoint_counters(window),
            experiment_critical_sampling=critical_quality(quality),
            containment=containment.get('valid') is True and not containment.get('violations'),
            cleanup=cleanup.get('empty_and_removed') is True and recovery.get('action')=='already_clean',
            no_guardrail_hit=bool(guard) and guard.get('stop_reason') is None
                and guard.get('pids_events',{}).get('max')==0,
            no_execution_exception=not (run/'failed.json').exists() and result.get('process_returncode') in (0,2),
            no_upstream_requests=s.get('upstream_requests')==0)
        report['primary_valid']=all(report['checks'].values())
        report['auxiliary_diagnostics_valid']=diagnostic_quality(quality) and all(t['auxiliary_diagnostics'] for t in report['tasks'])
        report['experiment_auxiliary_diagnostics_valid']=diagnostic_quality(quality)
        report['excluded_claims_when_auxiliary_invalid']=['per-process RSS/IO/CPU phase attribution from process snapshots',
            'instantaneous leaf cgroup decomposition from 200 ms diagnostics']
    except (KeyError,TypeError,ValueError,FileNotFoundError) as exc:
        report['errors'].append(type(exc).__name__+': '+str(exc))
    return apply_contract(report,contract)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('run',type=Path)
    a=p.parse_args()
    print(json.dumps(assess(a.run),indent=2,ensure_ascii=False))
