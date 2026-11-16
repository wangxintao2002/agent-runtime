"""Observation outside the workload domain; missing metrics remain missing.

Task cgroup counters are the primary attribution source. /proc/diskstats is
host-wide context, not task I/O. Live-process counters are diagnostic snapshots,
not totals: parents include waited children, so summing PID histories is wrong.
"""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import threading
import time

from counters import counters


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def host_snapshot():
    stat = Path("/proc/stat").read_text().splitlines()
    cpu = {s.split()[0]: list(map(int, s.split()[1:])) for s in stat if s.startswith("cpu")}
    runnable = {s.split()[0]: int(s.split()[1]) for s in stat if s.startswith(("procs_running", "procs_blocked"))}
    mem = {s.split(":")[0]: int(s.split()[1]) * 1024 for s in Path("/proc/meminfo").read_text().splitlines()
           if s.startswith(("MemAvailable:", "MemTotal:", "SwapFree:", "Dirty:", "Writeback:"))}
    disks = {}
    for line in Path("/proc/diskstats").read_text().splitlines():
        fields = line.split()
        if len(fields) >= 14 and not fields[2].startswith(("loop", "ram")):
            disks[fields[2]] = list(map(int, fields[3:]))
    return {"cpu_ticks": cpu, "processes": runnable, "memory": mem, "diskstats": disks}


class ProcessIO:
    def __init__(self):
        self.races = 0
        self.errors = Counter()

    def sample(self, groups):
        states = Counter()
        live = 0
        snapshots = []
        for group in groups:
            for pid in (group / "cgroup.procs").read_text().split():
                try:
                    path = Path("/proc") / pid
                    fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                    # fields starts with original stat field 3 (state).
                    identity = (pid, fields[19])
                    io = {k.rstrip(":"): int(v) for k, v in
                          (line.split() for line in (path / "io").read_text().splitlines())}
                    snapshots.append({"pid": int(pid), "start_ticks": int(identity[1]),
                                      "ppid": int(fields[1]), "state": fields[0], "io": io})
                    states[fields[0]] += 1
                    live += 1
                except (FileNotFoundError, ProcessLookupError):
                    self.races += 1
                except (OSError, ValueError, IndexError) as exc:
                    self.errors[type(exc).__name__] += 1
        return {"live_processes_seen": live, "states": dict(states),
                "per_process_including_waited_children_do_not_sum": snapshots,
                "exited_process_read_races": self.races, "errors": dict(self.errors)}


class Observer:
    def __init__(self, pool, groups, out, interval_s=.05, process_interval_s=.2):
        self.pool, self.groups, self.out = pool, groups, out
        self.interval = interval_s
        self.process_interval = process_interval_s
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.errors = []
        self.timestamps, self.costs = [], []
        self.cpu_s = 0
        self.io = ProcessIO()

    def start(self):
        # Fail before launching work if critical observation is unavailable.
        baseline = counters(self.pool)
        for field in ("memory.current", "cpu.stat", "memory.events", "cpu.pressure", "memory.pressure", "io.pressure"):
            if baseline[field] is None:
                raise RuntimeError(f"Required observation missing: {field}")
        self.thread.start()

    def run(self):
        cpu_start = time.thread_time()
        next_due = time.monotonic()
        last_slow = 0
        with (self.out / "resources.jsonl").open("w", buffering=1) as stream:
            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    row = {"monotonic_s": started, "pool": counters(self.pool)}
                    # All task cgroups exist before sampling and are removed only
                    # after the observer stops, so disappearance is an error.
                    row["tasks"] = {name: {"memory.current": int((group / "memory.current").read_text()),
                                           "cpu.stat": (group / "cpu.stat").read_text()}
                                    for name, group in self.groups.items()}
                    if started - last_slow >= self.process_interval:
                        row["processes"] = self.io.sample(self.groups.values())
                        row["host"] = host_snapshot()
                        last_slow = started
                    row["read_cost_s"] = time.monotonic() - started
                    stream.write(json.dumps(row) + "\n")
                    self.timestamps.append(started)
                    self.costs.append(time.monotonic() - started)
                except Exception as exc:
                    self.errors.append({"monotonic_s": started, "error": repr(exc)})
                next_due += self.interval
                now = time.monotonic()
                if next_due < now:
                    next_due = now
                self.stop_event.wait(max(0, next_due - now))
        self.cpu_s = time.thread_time() - cpu_start

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("Observer failed to stop")
        gaps = [b - a for a, b in zip(self.timestamps, self.timestamps[1:])]
        quality = {"sample_count": len(self.timestamps), "requested_interval_s": self.interval,
                   "gap_p50_s": percentile(gaps, .5), "gap_p95_s": percentile(gaps, .95),
                   "gap_max_s": max(gaps, default=0), "read_cost_p95_s": percentile(self.costs, .95),
                   "observer_thread_cpu_s": self.cpu_s, "errors": self.errors,
                   "process_io_errors": dict(self.io.errors), "process_io_exit_races": self.io.races,
                   "process_io_is_diagnostic_snapshot_not_sum": True,
                   "valid": not self.errors and len(gaps) > 2 and percentile(gaps, .95) <= self.interval * 2
                            and max(gaps, default=0) <= self.interval * 5}
        (self.out / "observation-quality.json").write_text(json.dumps(quality, indent=2) + "\n")
        return quality
