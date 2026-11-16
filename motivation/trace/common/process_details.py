"""Process diagnostics without reading command lines or environments."""
import os
from pathlib import Path
from observe import ProcessIO

class ProcessDetails(ProcessIO):
    def __init__(self, allowed_cpus=None):
        super().__init__()
        self.allowed_cpus = set(allowed_cpus) if allowed_cpus else None
        self.clock_ticks = os.sysconf("SC_CLK_TCK")
        self.affinity_checked_threads = 0
        self.affinity_violations = []

    def sample(self, groups):
        result = super().sample(groups)
        for process in result["per_process_including_waited_children_do_not_sum"]:
            try:
                path = Path("/proc") / str(process["pid"])
                # No cmdline or environment: these can contain credentials.
                status = (path / "status").read_text().splitlines()
                stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
                process["cpu_self_s"] = (int(stat[11]) + int(stat[12])) / self.clock_ticks
                process["cpu_waited_children_s_do_not_sum"] = (int(stat[13]) + int(stat[14])) / self.clock_ticks
                process["nice"] = os.getpriority(os.PRIO_PROCESS, process["pid"])
                process["comm"] = (path / "comm").read_text().strip()
                process["cpus_allowed_list"] = next(
                    (line.split(":", 1)[1].strip() for line in status if line.startswith("Cpus_allowed_list:")), None)
                process["memory_bytes"] = {line.split(":")[0]: int(line.split()[1]) * 1024
                                           for line in status if line.startswith(
                                               ("VmRSS:", "RssAnon:", "RssFile:", "RssShmem:"))}
                if self.allowed_cpus is not None:
                    # Libraries may set individual thread affinities themselves.
                    # Detect sampled escapes instead of assuming taskset is a hard cpuset.
                    for thread in (path / "task").iterdir():
                        try:
                            cpus = os.sched_getaffinity(int(thread.name))
                            self.affinity_checked_threads += 1
                            if not cpus.issubset(self.allowed_cpus):
                                self.affinity_violations.append({"tid": int(thread.name), "cpus": sorted(cpus)})
                        except ProcessLookupError:
                            pass
            except (FileNotFoundError, ProcessLookupError):
                process["memory_read_race"] = True
        return result
