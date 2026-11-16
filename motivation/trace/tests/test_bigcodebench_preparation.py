import unittest
from prepare_bigcodebench import select_tasks


def candidates():
    return [dict(task_id=f'BigCodeBench/{i}',instruct_prompt=f'Original instruction {i}\n',
                 entry_point='task_func',canonical_solution='HIDDEN_REFERENCE',test='HIDDEN_TEST',
                 unexpected_field='MUST_NOT_BECOME_AGENT_INPUT') for i in range(12)]


class PreparationTests(unittest.TestCase):
    def test_public_payload_excludes_reference_tests_and_unknown_fields(self):
        public,reference=select_tasks(candidates(),8)
        self.assertEqual(set(public[0]),{'id','benchmark','version','variant','prompt','entry_point'})
        self.assertNotIn('HIDDEN_',str(public))
        self.assertNotIn('MUST_NOT',str(public))
        self.assertIn('HIDDEN_REFERENCE',str(reference))
        self.assertTrue(public[0]['prompt'].endswith('\n'))

    def test_selection_is_order_independent_and_prefix_stable(self):
        rows=candidates();small,_=select_tasks(rows,4);large,_=select_tasks(list(reversed(rows)),8)
        self.assertEqual(small,large[:4])
        self.assertEqual(len({t['id'] for t in large}),8)

    def test_duplicates_and_oversize_cohorts_fail_without_replacement(self):
        rows=candidates()
        with self.assertRaises(ValueError):select_tasks(rows+[rows[0]],4)
        with self.assertRaises(ValueError):select_tasks(rows,13)


if __name__=='__main__':
    unittest.main()
