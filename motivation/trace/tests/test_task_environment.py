from pathlib import Path
import sys
import tempfile
import unittest

from run_pilot import verify_import_paths, candidate_patch
import subprocess


class EnvironmentTests(unittest.TestCase):
    def test_package_internal_tests_never_enter_candidate_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pkg/tests").mkdir(parents=True)
            (repo / "pkg/test").mkdir()
            paths = [repo / "pkg/code.py", repo / "pkg/tests/test_code.py", repo / "pkg/test/test_code.py"]
            for path in paths:
                path.write_text("value = 1\n")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@localhost",
                            "commit", "-qm", "baseline"], check=True)
            for path in paths:
                path.write_text("value = 2\n")
            patch = candidate_patch(repo, {"source_paths": ["pkg"]})
            self.assertIn("pkg/code.py", patch)
            self.assertNotIn("pkg/tests/test_code.py", patch)
            self.assertNotIn("pkg/test/test_code.py", patch)

    def test_src_layout_cannot_import_cached_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, cache = root / "workspace", root / "cache"
            for package in (repo / "src/taskpkg", cache / "taskpkg"):
                package.mkdir(parents=True)
                (package / "__init__.py").write_text("")
            spec = {"import_modules": ["taskpkg"], "pythonpath": [str(cache)], "source_paths": ["src/taskpkg"]}
            with self.assertRaisesRegex(ValueError, "outside its own checkout"):
                verify_import_paths(repo, Path(sys.executable), spec)
            spec["pythonpath"] = ["src", "."]
            origins = verify_import_paths(repo, Path(sys.executable), spec)
            self.assertEqual(Path(origins["taskpkg"]), repo / "src/taskpkg/__init__.py")


if __name__ == "__main__":
    unittest.main()
