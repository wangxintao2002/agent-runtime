#!/usr/bin/env python3
"""Restore selected host tasks without applying reference source solutions."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
COMMON = HERE.parents[1] / 'common'
sys.path.insert(0, str(COMMON))
from paths import DATA, run_root
RUNS = run_root('swe')


def prepare(key):
    from task_spec import load_spec
    spec = load_spec(Path(key) if Path(key).suffix == ".json" else HERE / "task-specs" / (key + ".json"))
    task_file = Path(spec["task_file"])
    task = json.loads(task_file.read_text())
    source, python = Path(spec["source"]), Path(spec["python"])
    cache = source.parent
    cache.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        source.mkdir()
        for args in (["init", "-q"], ["remote", "add", "origin", f"https://github.com/{task['repo']}.git"],
                     ["fetch", "--depth=1", "origin", task["base_commit"]],
                     ["checkout", "--detach", "FETCH_HEAD"]):
            subprocess.run(["git", *args], cwd=source, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if head != task["base_commit"]:
        raise ValueError("Cached base commit mismatch")
    requirements = [line for line in task["requirements"].splitlines() if line and not line.startswith("-e ")]
    adaptations = []
    if any(" @ file:" in line for line in requirements):
        if not spec.get("restore_conda_versions"):
            raise ValueError("Local dependency path needs explicit reviewed adaptation")
        versions = {name.lower().replace("_", "-"): version for name, version in
                    re.findall(r"^  - ([\w-]+)=([^=\n]+)=", task["environment"], re.M)}
        restored = []
        for line in requirements:
            if " @ file:" in line:
                name = line.split(" @ ")[0]
                replacement = name + "==" + versions[name.lower().replace("_", "-")]
                adaptations.append({"original": line, "replacement": replacement,
                                    "reason": "Restore same package version from recorded Conda environment"})
                line = replacement
            restored.append(line)
        requirements = restored
    req = cache / "requirements.txt"
    req.write_text("\n".join(requirements) + "\n")
    if not python.exists():
        if spec.get("environment_builder") == "conda":
            subprocess.run([spec["conda_executable"], "create", "-p", str(python.parent.parent), "--override-channels",
                            "-c", "https://repo.anaconda.com/pkgs/main", "python=" + spec["host_python"],
                            "pip=" + spec.get("conda_seed_pip", "22.3.1"), "-y"], check=True)
        else:
            subprocess.run(["uv", "venv", "--python", spec.get("host_python", "3.9.21"), str(python.parent.parent)], check=True)
    install = ([str(python), "-m", "pip", "install"] if spec.get("environment_builder") == "conda"
               else ["uv", "pip", "install", "--python", str(python)])
    build_env = {**os.environ, **spec.get("build_environment", {})}
    subprocess.run([*install, "-r", str(req),
                    *spec.get("build_tools", ["pip==25.0", "setuptools==65.6.3", "wheel==0.45.1"])], check=True, env=build_env)
    subprocess.run([*install, "--no-deps",
                    "--no-build-isolation", "-e", str(source)], check=True, env=build_env)
    freeze = ([str(python), "-m", "pip", "freeze"] if spec.get("environment_builder") == "conda"
              else ["uv", "pip", "freeze", "--python", str(python)])
    (cache / "installed.txt").write_bytes(subprocess.check_output(freeze))
    (cache / "preparation.json").write_text(json.dumps({"task": task["instance_id"], "base_commit": head,
        "task_sha256": hashlib.sha256(task_file.read_bytes()).hexdigest(), "gold_patch_used": False,
        "dependency_adaptations": adaptations,
        "build_environment": spec.get("build_environment", {}),
        "host_python": subprocess.check_output([str(python), "--version"], stderr=subprocess.STDOUT, text=True).strip(),
        "dependency_policy": "Recorded pinned requirements; editable repository restored at base commit; build tooling pinned"}, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("tasks", nargs="+", help="reviewed task key or JSON spec path")
    for key in parser.parse_args().tasks:
        prepare(key)
