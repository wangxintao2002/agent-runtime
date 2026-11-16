from pathlib import Path
import unittest
from replay_paths import replay_run_name


class ReplayPathTests(unittest.TestCase):
    def test_equal_full_path_lengths_and_unique_names_for_distinct_attempts(self):
        root=Path('/workspace/runs')
        source=root/'a-recorded-task-with-long-output'
        names=[replay_run_name('a-very-long-batch-prefix-'+str(i),source,root) for i in range(128)]
        self.assertEqual(len(set(names)),128)
        for name in names:
            self.assertEqual(len(str(root/name/'workspace')),len(str(source/'workspace')))
            self.assertTrue(all(c.isalnum() or c in '-_' for c in name))
        short=replay_run_name('short',source,root)
        self.assertEqual(len(short),len(source.name))
        self.assertNotEqual(short,replay_run_name('short_',source,root))

    def test_unsupported_path_length_fails_instead_of_changing_output_boundaries(self):
        with self.assertRaisesRegex(ValueError,'preserve'):
            replay_run_name('test',Path('/a/b'),Path('/a'))


if __name__ == '__main__':
    unittest.main()
