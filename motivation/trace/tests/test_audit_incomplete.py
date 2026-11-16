import json
from pathlib import Path
import tempfile
import unittest

from audit_replay import audit


class InterruptedAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source, self.run = root/'source', root/'replay'
        raw = b'{"input_tokens":17}'
        for directory, start in ((self.source, 10.), (self.run, 20.)):
            request = directory/'requests/0001'
            request.mkdir(parents=True)
            (directory/'events.jsonl').write_text('')
            for name in ('candidate.patch', 'all-changes.patch'):
                (directory/name).write_text('')
            for name, value in [('metadata.json', {}), ('validation.json', {'after': None})]:
                (directory/name).write_text(json.dumps(value))
            (request/'request.json').write_text(json.dumps({'stream': False, 'messages': []}))
            (request/'response.bin').write_bytes(raw)
            (request/'meta.json').write_text(json.dumps(dict(status=200, error=None, start_s=start)))
            (request/'chunks.jsonl').write_text(json.dumps(dict(offset=0, length=len(raw), monotonic_s=start+1))+'\n')
        (self.run/'replay-validation.json').write_text(json.dumps(dict(source=str(self.source), executor='claude', delay_scale=1)))

    def test_complete_evidence_remains_valid(self):
        self.assertTrue(audit(self.run)['raw_evidence_matches'])

    def test_interrupted_response_without_metadata_returns_failure(self):
        path = self.run/'requests/0001/meta.json'
        path.unlink()
        result = audit(self.run)
        self.assertFalse(result['raw_evidence_matches'])
        self.assertFalse(result['execution_evidence_matches'])
        self.assertFalse(result['audit_complete'])
        self.assertEqual(result['evidence_read_error']['file'], str(path))

    def test_partial_json_is_failed_evidence(self):
        (self.run/'requests/0001/meta.json').write_text('{')
        result = audit(self.run)
        self.assertFalse(result['raw_evidence_matches'])
        self.assertEqual(result['evidence_read_error']['type'], 'JSONDecodeError')

    def test_different_response_bytes_still_fail(self):
        (self.run/'requests/0001/response.bin').write_bytes(b'{"input_tokens":18}')
        self.assertFalse(audit(self.run)['raw_evidence_matches'])


if __name__ == '__main__':
    unittest.main()
