"""Freeze a public BigCodeBench Instruct cohort without executing reference code.

Preparation is separate from recording and performance measurement. Candidate
selection uses only task identity, never correctness, latency or resource cost.
Only tasks.json is intended as input to a future recording adapter; reference
solutions/tests and the complete source parquet stay on the controller side.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import urllib.request

REVISION='b74c0d0bf70d2c0bc459be537895cca163007f1a'
VERSION='v0.1.4'
SEED='bigcodebench-20261019'
URL=f'https://huggingface.co/datasets/bigcode/bigcodebench/resolve/{REVISION}/data/{VERSION}-00000-of-00001.parquet'


def select_tasks(rows, count):
    by_id={}
    for row in rows:
        identity=row['task_id']
        if not re.fullmatch(r'BigCodeBench/[0-9]+',identity):
            raise ValueError('Unexpected task identity')
        if identity in by_id:
            raise ValueError('Duplicate task identity')
        for key in ('instruct_prompt','entry_point','canonical_solution','test'):
            if not isinstance(row.get(key),str) or not row[key].strip():
                raise ValueError('Missing required field: '+key+' for '+identity)
        by_id[identity]=row
    if not 1<=count<=len(by_id):
        raise ValueError('Candidate count must fit the distinct source dataset')
    identities=sorted(by_id,key=lambda identity:hashlib.sha256((SEED+':'+identity).encode()).hexdigest())[:count]
    public=[dict(id=identity,benchmark='BigCodeBench',version=VERSION,variant='Instruct',
                 prompt=by_id[identity]['instruct_prompt'],entry_point=by_id[identity]['entry_point'])
            for identity in identities]
    controller=[dict(id=identity,canonical_solution=by_id[identity]['canonical_solution'],
                     test=by_id[identity]['test']) for identity in identities]
    return public,controller


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',required=True,type=Path)
    p.add_argument('--source-parquet',type=Path,help='Already downloaded pinned source; otherwise download the fixed public URL')
    p.add_argument('--task-limit',type=int,default=128)
    a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False)
    controller=a.out/'controller-only';controller.mkdir()
    source=controller/'source.parquet'
    if a.source_parquet:
        shutil.copy2(a.source_parquet,source)
    else:
        with urllib.request.urlopen(URL,timeout=60) as response,source.open('wb') as stream:
            shutil.copyfileobj(response,stream)
    import pyarrow.parquet as pq
    rows=pq.read_table(source).to_pylist()
    public,reference=select_tasks(rows,a.task_limit)
    def save(path,value):
        path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    save(a.out/'tasks.json',public)
    save(controller/'reference.json',reference)
    save(a.out/'manifest.json',dict(benchmark='BigCodeBench',variant='Instruct',version=VERSION,
        hf_revision=REVISION,source_url=URL,source_rows=len(rows),selected_tasks=len(public),
        source_input='supplied local parquet' if a.source_parquet else 'downloaded pinned public URL',
        local_input_requires_provenance_check=bool(a.source_parquet),
        selection='first task-limit SHA256(seed + colon + task_id), no replacement or outcome/cost filtering',
        selection_seed=SEED,official_paper_setup_identical=False,
        agent_payload_allowlist=['id','benchmark','version','variant','prompt','entry_point'],
        reference_access='controller only; never copy source parquet, reference.json or controller directory into Agent workspace',
        execution='no reference code executed during preparation; use run_bigcodebench.py for real Agent recording',
        recorded_agent_traces=0,
        files={str(f.relative_to(a.out)):hashlib.sha256(f.read_bytes()).hexdigest()
               for f in (source,a.out/'tasks.json',controller/'reference.json')}))
    print(json.dumps(dict(data=str(a.out.resolve()),tasks=len(public),recorded_agent_traces=0)))


if __name__=='__main__':
    main()
