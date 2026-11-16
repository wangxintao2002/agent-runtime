#!/usr/bin/env python3
"""One online public software task, real Claude tools, independent acceptance."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
COMMON = HERE.parents[1] / 'common'
sys.path.insert(0, str(COMMON))
from paths import DATA, run_root
RUNS = run_root('swe')
from observe import host_snapshot
from process_details import ProcessDetails
from process_observe import Observer
from counters import counters
from launch import MODEL, child_environment
from record import Recorder, save
from acceptance_evidence import assess, defect_status, node_matches, outcomes


def git(repo, *args, **kwargs):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True,
                          capture_output=True, **kwargs).stdout


def clone(source, target):
    subprocess.run(["git", "-c", "advice.detachedHead=false", "clone", "-q", "--no-hardlinks",
                    str(source), str(target)], check=True)


def candidate_patch(repo, spec):
    # Some packages keep upstream tests inside their importable source tree.
    # Agent-edited tests must never overwrite the independent evaluator's tests.
    return git(repo, "diff", "--binary", "HEAD", "--", *spec["source_paths"],
               ":(glob,exclude)**/tests/**", ":(glob,exclude)**/testsuite/**", ":(glob,exclude)**/test/**")


def verify_import_paths(repo, python, spec):
    modules = spec.get("import_modules", [])
    env = {"PATH": f"{python.parent}:/usr/bin:/bin", "LANG": "C.UTF-8",
           "PYTHONPATH": os.pathsep.join(str(repo / p) for p in spec["pythonpath"])}
    code = "import importlib.util,json,sys; print(json.dumps({m:importlib.util.find_spec(m).origin for m in sys.argv[1:]}))"
    origins = json.loads(subprocess.check_output([str(python), "-c", code, *modules], cwd=repo, env=env, text=True))
    for module, origin in origins.items():
        if not origin or not Path(origin).resolve().is_relative_to(repo.resolve()):
            raise ValueError("Task imports outside its own checkout: " + module)
    if any(not (repo / p).exists() for p in spec["source_paths"]):
        raise ValueError("Configured candidate source path does not exist")
    return origins


def verify(repo, python, task, log, spec):
    env = {"PATH": f"{python.parent}:/usr/bin:/bin", "LANG": "C.UTF-8",
           "PYTHONPATH": os.pathsep.join(str(repo / p) for p in spec["pythonpath"]), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    plugins = [arg for name in spec["pytest_plugins"] for arg in ("-p", name)]
    with log.open("w") as stream:
        result = subprocess.run([str(python), "-m", "pytest", "-v", "--tb=short", *plugins, *spec["test_args"]],
                                cwd=repo, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=120)
    output = log.read_text()
    report = {"returncode": result.returncode,
            "counts": re.findall(r"\b\d+ (?:passed|failed|errors?|skipped)\b", output),
            "defect_tests": {name: bool(re.search(re.escape(name) + r"\s+PASSED", output))
                             for name in task["FAIL_TO_PASS"]}}
    if spec.get("resolve_truncated_test_nodes"):
        report["defect_tests"] = defect_status(task, output, "PASSED")
        report["resolved_defect_nodes"] = {n: sorted(node_matches(n, outcomes(output))) for n in task["FAIL_TO_PASS"]}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--task-spec", type=Path, help="reviewed task/environment/acceptance specification")
    parser.add_argument("--preflight-only", action="store_true", help="check checkout imports and baseline tests without a model request")
    parser.add_argument("--timeout-s", type=float, default=600)
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument("--replay-source", type=Path, help="offline recorded model source; no API key used")
    parser.add_argument("--restore-recorded-git-objects", action="store_true",
                        help="replay only: restore identical base Git objects using recorded storage bytes")
    parser.add_argument("--delay-scale", type=float, default=1, help="recorded response delay multiplier; 0 is a sensitivity condition")
    parser.add_argument("--cgroup-parent", type=Path, help="optional shared batch resource domain")
    parser.add_argument("--direct-launch", action="store_true", help="keep launch helpers inside an enclosing experiment cgroup")
    parser.add_argument("--start-gate", type=Path, help="finish preparation, write ready.json, wait for this file")
    parser.add_argument("--acceptance-gate", type=Path, help="defer independent tests until all batch agents have exited")
    parser.add_argument("--gate-timeout-s", type=float, default=120, help="bounded preparation/queue gate wait")
    parser.add_argument("--agent-cpus", help="comma-separated allowed CPUs for the agent and tools only")
    args = parser.parse_args()
    if args.restore_recorded_git_objects and not args.replay_source:
        parser.error("Recorded Git object restoration requires offline replay")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.name) or args.timeout_s <= 0 or args.max_requests < 1 or args.delay_scale < 0 or not 0 < args.gate_timeout_s <= 86400:
        parser.error("Invalid name/timeout/request limit")
    agent_cpus = sorted(os.sched_getaffinity(0))
    if args.agent_cpus:
        try:
            agent_cpus = sorted(set(map(int, args.agent_cpus.split(","))))
        except ValueError:
            parser.error("Invalid agent CPU list")
        if not agent_cpus or not set(agent_cpus).issubset(os.sched_getaffinity(0)):
            parser.error("Agent CPUs must be within the current allowed set")
    key = (os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DS_API_KEY")) if not args.replay_source else None
    if not key and not args.replay_source and not args.preflight_only:
        parser.error("请通过 shell 加载并导出 DEEPSEEK_API_KEY 或 DS_API_KEY；不打印密钥。")
    if args.replay_source:
        args.replay_source = args.replay_source.resolve()
        source_summary = json.loads((args.replay_source / "summary.json").read_text())
        if not source_summary.get("trace_complete", source_summary.get("pilot_valid", False)):
            parser.error("Replay source must have complete validated execution evidence")
    binary = shutil.which("claude")
    if not binary:
        parser.error("Claude Code is not installed")
    out = RUNS / args.name
    out.mkdir(parents=True, mode=0o700, exist_ok=False)
    spec_path = args.task_spec or (args.replay_source / "task-spec.json" if args.replay_source and
                                  (args.replay_source / "task-spec.json").exists() else HERE / "task-specs/universe.json")
    from task_spec import load_spec
    spec = load_spec(spec_path)
    if args.replay_source and (args.replay_source / "task-spec.json").exists():
        if spec != json.loads((args.replay_source / "task-spec.json").read_text()):
            raise ValueError("Replay task specification differs from recording")
    save(out / "task-spec.json", spec)
    task = json.loads(Path(spec["task_file"]).read_text())
    source, python = Path(spec["source"]), Path(spec["python"])
    if args.replay_source and json.loads((args.replay_source / "metadata.json").read_text())["task"] != task["instance_id"]:
        raise ValueError("Replay task identity mismatch")
    repo, acceptance = out / "workspace", out / "acceptance"
    clone(source, repo)
    clone(source, acceptance)
    if args.restore_recorded_git_objects:
        from replay_git_objects import restore_object_storage
        save(out / "git-object-storage-restoration.json",
             restore_object_storage(args.replay_source / "workspace", source, repo))
    # Some legacy packages generate ignored version modules during installation.
    # Restore only reviewed, hash-pinned build outputs into both fresh checkouts.
    for relative, expected_hash in spec.get("prepared_files", {}).items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Prepared file must stay within the checkout")
        content = (source / relative).read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise ValueError("Prepared build output changed: " + relative)
        for checkout in (repo, acceptance):
            target = checkout / relative
            if target.exists():
                raise ValueError("Prepared file must not overwrite tracked source")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    assert git(repo, "rev-parse", "HEAD").strip() == task["base_commit"]
    save(out / "import-paths.json", {"workspace": verify_import_paths(repo, python, spec),
                                    "acceptance": verify_import_paths(acceptance, python, spec)})
    # Only the evaluator sees the official test patch; never use task['patch'].
    git(acceptance, "apply", "-", input=task["test_patch"].rstrip("\n") + "\n")
    before_test = verify(acceptance, python, task, out / "acceptance-before.log", spec)
    save(out / "acceptance-before.json", before_test)
    before_text = (out / "acceptance-before.log").read_text()
    defects_failed = all(defect_status(task, before_text, "FAILED").values()) if spec.get("resolve_truncated_test_nodes") else all(
        re.search(re.escape(name) + r"\s+FAILED", (out / "acceptance-before.log").read_text())
        for name in task["FAIL_TO_PASS"]
    )
    if before_test["returncode"] != 1 or not task["FAIL_TO_PASS"] or not defects_failed:
        raise RuntimeError("Defect not reproduced; do not start an online run")
    if spec.get("require_declared_baseline"):
        baseline = assess(task, before_text, before_text, before_test["returncode"], before_test["returncode"])
        save(out / "baseline-evidence.json", baseline)
        if not baseline["baseline_valid"]:
            raise RuntimeError("Declared baseline not reproduced; do not start an online run")
    if args.preflight_only:
        save(out / "preflight.json", {"valid": True, "task": task["instance_id"], "model_requests": 0,
                                     "before": before_test, "declared_baseline_checked": bool(spec.get("require_declared_baseline"))})
        print(json.dumps({"preflight_valid": True, "task": task["instance_id"]}))
        return 0
    prompt = (
        "Fix this issue in the current repository:\n\n" + task["problem_statement"] + "\n\n"
        "Inspect the source, implement the fix, and run the existing tests with `" + spec["agent_test_command"] + "`. "
        "Dependencies are already installed and the correct Python is first on PATH. "
        "You may add a focused regression test. Do not install dependencies, access the network, "
        "read files outside this task repository, read environment variables/credentials, "
        "use other agents, or commit. Use foreground tools only (no background jobs). "
        "Finish with a brief explanation of the change and actual test results."
    )
    (out / "prompt.txt").write_text(prompt)
    profile = out / "profile"
    profile.mkdir(mode=0o700)
    (out / "source").mkdir()
    for file in [*HERE.glob("*.py"), *COMMON.glob("*.py")]:
        shutil.copyfile(file, out / "source" / file.name)
    uid = os.getuid()
    delegated = Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service")
    if args.cgroup_parent:
        delegated = args.cgroup_parent.resolve()
    root = delegated / ("claude_pilot_" + uuid.uuid4().hex[:12])
    root.mkdir()
    group = root / "agent"
    server = observer = proc = None
    unit = "claude-pilot-" + uuid.uuid4().hex + ".scope"
    started = ended = None
    try:
        for name, value in {"memory.max": 8 * 1024**3, "memory.swap.max": 0,
                            "pids.max": 4096, "cgroup.subtree_control": "+memory +pids"}.items():
            (root / name).write_text(str(value))
        group.mkdir()
        if args.replay_source:
            from replay import ReplayServer
            server = ReplayServer(out, args.replay_source, repo, args.delay_scale, "claude")
        else:
            server = Recorder(out, key, args.max_requests)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        settings = {"hooks": {event: [{"hooks": [{"type": "http", "url": server.url + "/hooks",
                        "headers": {"Authorization": "Bearer " + server.token}, "timeout": 5}]}]
                    for event in ("SessionStart", "SessionEnd", "PreToolUse", "PostToolUse",
                                  "PostToolUseFailure", "Stop")}}
        save(out / "settings.json", settings)
        base_env = child_environment(server.token)
        # Only this local recorder gets the real key. Tools receive a local token.
        keep = {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "PATH",
                "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "SSL_CERT_FILE", "SSL_CERT_DIR"}
        env = {k: v for k, v in base_env.items()
               if k in keep or k.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))}
        env.update({"ANTHROPIC_BASE_URL": server.url + "/anthropic", "CLAUDE_CONFIG_DIR": str(profile),
                    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "PATH": str(python.parent) + ":" + env["PATH"],
                    "PYTHONPATH": os.pathsep.join(str(repo / p) for p in spec["pythonpath"]),
                    "VIRTUAL_ENV": str(python.parent.parent), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    "TMPDIR": str(out / "tmp")})
        (out / "tmp").mkdir()
        command = [binary, "--setting-sources", "", "--settings", str(out / "settings.json"),
                   "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--model", MODEL,
                   "--no-chrome", "--disable-slash-commands", "--permission-mode", "dontAsk",
                   "--tools", "Read,Glob,Grep,Edit,Write,Bash", "--allowedTools", "Read,Glob,Grep,Edit,Write,Bash",
                   "--output-format", "stream-json", "--verbose", "--include-partial-messages", "-p", prompt]
        entry = COMMON / "enter_cgroup.py"
        marker = out / "entered"
        launch = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit=" + unit,
                  "taskset", "--cpu-list", ",".join(map(str, agent_cpus)),
                  sys.executable, str(entry), str(group), str(marker), *command]
        if args.direct_launch:
            current_group = Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1].lstrip("/")
            if not args.cgroup_parent or current_group.parent != args.cgroup_parent.resolve().parent:
                raise ValueError("Direct launch requires a controller leaf beside the batch pool")
            launch = launch[6:]
        save(out / "metadata.json", {
            "date_utc": datetime.now(timezone.utc).isoformat(), "task": task["instance_id"],
            "executor": "claude",
            "mode": "model_replay" if args.replay_source else "online",
            "replay_source": str(args.replay_source) if args.replay_source else None,
            "response_delay_scale": args.delay_scale if args.replay_source else None,
            "base_commit": task["base_commit"], "model": MODEL,
            "task_key": spec["key"], "test_scope": spec["test_scope"],
            "claude_version": subprocess.check_output([binary, "--version"], text=True).strip(),
            "python": str(python), "cpu_affinity": sorted(os.sched_getaffinity(0)),
            "tool_locale": {"LANG": env["LANG"], "LC_ALL": env["LC_ALL"]},
            "workspace_path_length": len(str(repo)),
            "agent_cpus": agent_cpus,
            "restore_recorded_git_objects": args.restore_recorded_git_objects,
            "memory_max_bytes": 8 * 1024**3, "sample_interval_s": .05,
            "timeout_s": args.timeout_s, "max_model_requests": args.max_requests,
            "scope": "single online calibration; agent + descendants; recorder/observer/evaluation excluded",
            "gold_source_patch_used": False, "official_test_patch_visible_to_agent": False,
            "profile": str(profile), "cgroup": str(group), "host_before": host_snapshot(),
            "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (out / "source").glob("*.py")},
        })
        observer = Observer(root, {"agent": group}, out)
        observer.io = ProcessDetails(agent_cpus if set(agent_cpus) != os.sched_getaffinity(0) else None)
        observer.prepare()
        if args.start_gate:
            save(out / "ready.json", {"group": str(group), "root": str(root), "pid": os.getpid()})
            preparation_deadline = time.monotonic() + args.gate_timeout_s
            while not args.start_gate.exists():
                if time.monotonic() > preparation_deadline:
                    raise TimeoutError("Batch did not release start gate")
                time.sleep(.01)
        observer.start()
        baseline = counters(root)
        started = time.monotonic()
        server.event("agent_launch")
        with (out / "claude.jsonl").open("w") as stdout, (out / "stderr.log").open("w") as stderr:
            # Claude -p also consumes piped stdin. Never inherit a collector's
            # script/input and accidentally append it to the task prompt.
            proc = subprocess.Popen(launch, cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
            timed_out = False
            while proc.poll() is None or "populated 1" in (group / "cgroup.events").read_text():
                replay_failed = args.replay_source and server.failed.is_set()
                if time.monotonic() - started > args.timeout_s or replay_failed:
                    timed_out = not replay_failed
                    server.event("agent_replay_abort" if replay_failed else "agent_timeout")
                    (root / "cgroup.kill").write_text("1")
                    subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
                    break
                time.sleep(.05)
            proc.wait(timeout=10)
        ended = time.monotonic()
        server.event("agent_exit", returncode=proc.returncode, timed_out=timed_out)
        final = counters(root)
        quality = observer.stop()
        save(out / "cpu-affinity-check.json", {"expected_cpus": agent_cpus,
             "checked_thread_snapshots": observer.io.affinity_checked_threads,
             "violations": observer.io.affinity_violations,
             "scope": "sampled per-thread affinity; not continuous enforcement or a security sandbox"})
        if observer.io.affinity_violations:
            quality["valid"] = False
            quality["affinity_violation"] = True
            save(out / "observation-quality.json", quality)
        observer = None
        save(out / "window.json", {"start_s": started, "end_s": ended,
                                   "before": baseline, "after": final,
                                   "agent_pid": int(marker.read_text()) if marker.exists() else None,
                                   "returncode": proc.returncode, "timed_out": timed_out})
        if args.acceptance_gate:
            deadline = time.monotonic() + max(660, args.gate_timeout_s)
            while not args.acceptance_gate.exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("Batch acceptance gate was not released")
                time.sleep(.02)
        # Evaluate pristine upstream tests + hidden defect test against candidate SOURCE only.
        patch = candidate_patch(repo, spec)
        (out / "candidate.patch").write_text(patch)
        (out / "all-changes.patch").write_text(git(repo, "diff", "--binary", "HEAD"))
        git(acceptance, "apply", "-", input=patch) if patch else None
        after_test = verify(acceptance, python, task, out / "acceptance-after.log", spec)
        validation = {"before": before_test, "after": after_test,
                      "candidate_source_changed": bool(patch),
                      "changed_tracked_files": git(repo, "diff", "--name-only", "HEAD").splitlines(),
                      "untracked_files": git(repo, "ls-files", "--others", "--exclude-standard").splitlines(),
                      "independent_tests_pass": after_test["returncode"] == 0 and all(after_test["defect_tests"].values()),
                      "agent_returncode": proc.returncode, "timed_out": timed_out,
                      "resource_observation_valid": quality["valid"], "gold_source_patch_used": False}
        save(out / "validation.json", validation)
        if args.replay_source:
            replay_validation = server.report()
            replay_validation["source_patch_identical"] = patch == (args.replay_source / "candidate.patch").read_text()
            replay_validation["all_tracked_changes_identical"] = (out / "all-changes.patch").read_bytes() == (args.replay_source / "all-changes.patch").read_bytes()
            source_validation = json.loads((args.replay_source / "validation.json").read_text())
            replay_validation["acceptance_outcome_identical"] = after_test == source_validation["after"]
            replay_validation["complete"] = (replay_validation["complete"] and replay_validation["source_patch_identical"]
                                               and replay_validation["all_tracked_changes_identical"]
                                               and replay_validation["acceptance_outcome_identical"] and not validation["untracked_files"])
            save(out / "replay-validation.json", replay_validation)
        print(json.dumps(validation, indent=2), flush=True)
    finally:
        if proc:
            subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
        (root / "cgroup.kill").write_text("1")
        if proc:
            proc.wait(timeout=10)
        if observer:
            observer.stop()
        if server:
            server.shutdown()
            server.server_close()
        deadline = time.monotonic() + 10
        while "populated 1" in (root / "cgroup.events").read_text():
            if time.monotonic() > deadline:
                raise RuntimeError("Task cgroup still populated; preserving it")
            time.sleep(.05)
        if group.exists():
            group.rmdir()
        root.rmdir()
        if scratch is not None:
            # Only remove the unique directory allocated by this run, after all
            # agent descendants exit. Logs, patches, and tests remain in out.
            shutil.rmtree(scratch)
            (out / "tmp").unlink()
            save(out / "scratch-cleanup.json", {"path": str(scratch), "removed": True})
        save(out / "cleanup.json", {"cgroup_empty_and_removed": True})
    from summarize import summarize
    summary = summarize(out)
    save(out / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    # Collection success is execution/trace completion, never benchmark correctness.
    return 0 if summary["trace_complete"] else 2


if __name__ == "__main__":
    sys.exit(main())
