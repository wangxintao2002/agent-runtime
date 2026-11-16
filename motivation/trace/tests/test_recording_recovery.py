import json
from pathlib import Path
import tempfile
import unittest

from collect_bigcodebench import remaining_ranges
from recording_recovery import digest, partial_records


class RecordingContinuationTests(unittest.TestCase):
    def test_unattempted_ranges_exclude_failed_and_interrupted_attempts(self):
        tasks = [dict(id=str(i)) for i in range(12)]
        self.assertEqual(remaining_ranges(tasks, 2, 10, ['2','3','6','9','10']),
                         [(4,2),(7,2),(11,1)])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            remaining_ranges(tasks, 2, 10, ['2','2'])
        with self.assertRaisesRegex(ValueError, 'outside'):
            remaining_ranges(tasks, 2, 10, ['0'])

    def test_partial_census_rejects_changed_status_and_live_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); shard=root/'shard'; shard.mkdir(); run=root/'attempt'; run.mkdir()
            group=root/'removed-group'/'agent'
            rows=[dict(task_id='a',source=str(run))]
            (root/'status.json').write_text(json.dumps(dict(phase='stopped')))
            (shard/'status.json').write_text(json.dumps(dict(phase='stopped',tasks=rows)))
            (run/'metadata.json').write_text(json.dumps(dict(cgroup=str(group))))
            (run/'cleanup-recovery.json').write_text('{}')
            census=dict(shards=[str(shard)],tasks=rows,runs=[dict(path=str(run))],
                evidence_sha256={str(shard/'status.json'):digest(shard/'status.json')})
            (root/'attempt-census.json').write_text(json.dumps(census))
            self.assertEqual(partial_records(root), rows)
            group.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, 'cgroup remains'):partial_records(root)
            group.rmdir();group.parent.rmdir()
            (shard/'status.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'evidence changed'):partial_records(root)


if __name__=='__main__':unittest.main()
