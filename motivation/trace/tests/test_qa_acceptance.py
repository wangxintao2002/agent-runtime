"""Check independent QA scoring and artifact-change detection without model API."""
import json
from pathlib import Path
import tempfile
import unittest

from run_qa import evaluate, snapshot


class AcceptanceTests(unittest.TestCase):

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.workspace=Path(self.temp.name)/'workspace'
        self.workspace.mkdir()
        self.data=Path(self.temp.name)/'fixture'
        self.data.mkdir()
        self.gold=dict(_id='fixture',answer='Paris',supporting_facts=[['Example',0]])
        (self.data/'gold.evaluator-only.json').write_text(json.dumps([self.gold]))
        # Synthetic exact-match evaluator exercises the adapter contract, not
        # the external HotpotQA scoring implementation or actual dataset.
        (self.data/'official_evaluator.py').write_text(
            "def update_sp(m, actual, expected): m['sp_em']=float(actual==expected)\n"
            "def exact_match_score(a,b): return float(a==b)\n"
            "def f1_score(a,b): return (float(a==b),0,0)\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_output_fails(self):
        result=evaluate(self.data,self.gold['_id'],self.workspace)
        self.assertFalse(result['valid_output'])
        self.assertEqual(result['joint_em'],0.)

    def test_answer_correct_does_not_hide_missing_support(self):
        (self.workspace/'answer.json').write_text(json.dumps({'answer':self.gold['answer'],'supporting_facts':[]}))
        result=evaluate(self.data,self.gold['_id'],self.workspace)
        self.assertEqual(result['answer_em'],1.)
        self.assertEqual(result['joint_em'],0.)

    def test_matching_answer_and_support_reach_joint_match(self):
        (self.workspace/'answer.json').write_text(json.dumps({'answer':self.gold['answer'],'supporting_facts':self.gold['supporting_facts']}))
        self.assertEqual(evaluate(self.data,self.gold['_id'],self.workspace)['joint_em'],1.)

    def test_changed_answer_or_extra_file_changes_artifacts(self):
        path=self.workspace/'answer.json'
        path.write_text('{}');before=snapshot(self.workspace)
        path.write_text('{"answer":"changed"}')
        self.assertNotEqual(before,snapshot(self.workspace))
        path.write_text('{}');(self.workspace/'extra.txt').write_text('extra')
        self.assertNotEqual(before,snapshot(self.workspace))


if __name__=='__main__':
    unittest.main()
