"""Migration boundaries: independent checkout, real helper exec, fixture export."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from export_task import export_row
from task_spec import load_spec

ROOT = Path(__file__).resolve().parent.parent


class PortabilityTests(unittest.TestCase):
    def test_all_entrypoints_help_from_unrelated_cwd_without_pythonpath(self):
        spec = importlib.util.spec_from_file_location('trace_cli', ROOT / 'trace.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        environment = {k: v for k, v in os.environ.items()
                       if k not in ('PYTHONPATH', 'DS_API_KEY', 'DEEPSEEK_API_KEY')}
        with tempfile.TemporaryDirectory() as cwd:
            for command in module.COMMANDS:
                with self.subTest(command=command):
                    result = subprocess.run([sys.executable, str(ROOT / 'trace.py'), command, '--help'],
                        cwd=cwd, env=environment, capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('usage:', result.stdout)

    def test_spec_paths_follow_spec_location_without_dereferencing_venv_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'spec.json'
            venv_python = Path(tmp) / 'venv/bin/python'
            venv_python.parent.mkdir(parents=True)
            venv_python.symlink_to(sys.executable)
            path.write_text(json.dumps(dict(task_file='task.json', source='source', python='venv/bin/python')))
            loaded = load_spec(path)
            self.assertEqual(loaded['python'], str(venv_python))
            self.assertEqual(loaded['task_file'], str(Path(tmp) / 'task.json'))
            self.assertNotEqual(loaded['python'], str(venv_python.resolve()))

    def test_export_excludes_gold_patch_and_preserves_test_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = dict(instance_id='fixture-1', repo='fixture/repo', base_commit='abc',
                problem_statement='fixture issue', test_patch='test-only', requirements='',
                FAIL_TO_PASS='["test_failure"]', patch='REFERENCE_SOURCE_SHOULD_NOT_EXPORT')
            source = root / 'input.json'
            source.write_text(json.dumps(row))
            out = root / 'export'
            export_row(row, out, source)
            result = json.loads((out / 'task.json').read_text())
            self.assertNotIn('patch', result)
            self.assertEqual(result['FAIL_TO_PASS'], ['test_failure'])
            self.assertEqual(result['test_patch'], 'test-only')
            with self.assertRaises(FileExistsError):
                export_row(row, out, source)

    def test_launcher_enters_mock_group_before_exec_and_preserves_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / 'entered'
            # A fixture tests write-before-exec ordering, not kernel delegation.
            result = subprocess.run([sys.executable, str(ROOT / 'common/enter_cgroup.py'),
                str(root), str(marker), sys.executable, '-c', 'import os; print(os.getpid())'],
                capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), marker.read_text())
            self.assertEqual(marker.read_text(), (root / 'cgroup.procs').read_text())
