"""Export one public SWE-rebench row, excluding the reference source patch."""
import argparse
import hashlib
import json
from pathlib import Path


def export_row(row, out, source):
    required = ('instance_id', 'repo', 'base_commit', 'problem_statement',
                'test_patch', 'requirements', 'FAIL_TO_PASS')
    missing = [key for key in required if key not in row]
    if missing:
        raise ValueError('Missing task fields: ' + ', '.join(missing))
    task = {key: value for key, value in row.items() if key != 'patch'}
    # Some dataset exports encode the test lists as JSON strings.
    for key in ('FAIL_TO_PASS', 'PASS_TO_PASS', 'FAIL_TO_FAIL', 'PASS_TO_FAIL'):
        if isinstance(task.get(key), str):
            task[key] = json.loads(task[key])
    out.mkdir(parents=True, exist_ok=False)
    (out / 'task.json').write_text(json.dumps(task, ensure_ascii=False, indent=2) + '\n')
    (out / 'source.json').write_text(json.dumps({
        'input': str(source.resolve()), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'instance_id': task['instance_id'], 'reference_source_patch_exported': False,
        'selection': 'explicit instance ID; environment specification must be reviewed separately',
    }, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='local public parquet or single-task JSON')
    parser.add_argument('--instance-id', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.input.suffix == '.parquet':
        import pyarrow.parquet as pq
        rows = pq.read_table(args.input, filters=[('instance_id', '=', args.instance_id)]).to_pylist()
    else:
        value = json.loads(args.input.read_text())
        rows = value if isinstance(value, list) else [value]
    matches = [row for row in rows if row['instance_id'] == args.instance_id]
    if len(matches) != 1:
        parser.error('Expected exactly one matching task')
    export_row(matches[0], args.out, args.input)
    print(args.out.resolve() / 'task.json')


if __name__ == '__main__':
    main()
