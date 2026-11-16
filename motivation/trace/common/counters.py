"""Read cgroup v2 counters; missing kernel metrics stay explicit as None."""
from pathlib import Path

def counters(group: Path) -> dict:
    result = {}
    for name in ("memory.current", "memory.peak", "pids.current"):
        path = group / name
        result[name] = int(path.read_text()) if path.exists() else None
    for name in ("cpu.stat", "memory.stat", "memory.events"):
        path = group / name
        result[name] = ({k: int(v) for k, v in
                         (line.split() for line in path.read_text().splitlines())}
                        if path.exists() else None)
    for name in ("cpu.pressure", "memory.pressure", "io.pressure"):
        path = group / name
        result[name] = ({line.split()[0]: dict(field.split("=") for field in line.split()[1:])
                         for line in path.read_text().splitlines()} if path.exists() else None)
    path = group / "io.stat"
    result["io.stat"] = path.read_text() if path.exists() else None
    return result
