"""Independent observer process: replay validation cannot hold its Python GIL."""
from collections import Counter
import multiprocessing
import json
import resource
import time

from observe import ProcessIO


def worker(config, connection):
    from async_observe import Observer as Sampler
    from process_details import ProcessDetails
    observer = Sampler(config["pool"], config["groups"], config["out"],
                       interval_s=config["interval"], process_interval_s=config["process_interval"])
    observer.io = ProcessDetails(config["allowed_cpus"])
    observer.experiment_root = config.get("experiment_root")
    observer.extra_roots = config.get("extra_roots", {})
    started = False
    try:
        connection.send({"ready": True})
        if connection.recv() != "start":
            return
        usage = resource.getrusage(resource.RUSAGE_SELF)
        before_cpu = usage.ru_utime + usage.ru_stime
        observer.start()
        started = True
        connection.send({"started": True})
        if connection.recv() != "stop":
            raise RuntimeError("Unexpected observer command")
        quality = observer.stop()
        started = False
        usage = resource.getrusage(resource.RUSAGE_SELF)
        quality["observer_process_cpu_s"] = usage.ru_utime + usage.ru_stime - before_cpu
        quality["observer_process_isolated"] = True
        import json
        (config["out"] / "observation-quality.json").write_text(json.dumps(quality, indent=2)+"\n")
        connection.send({"quality": quality, "affinity_checked_threads": observer.io.affinity_checked_threads,
                         "affinity_violations": observer.io.affinity_violations,
                         "io_errors": dict(observer.io.errors), "io_races": observer.io.races})
    except EOFError:
        pass
    except Exception as exc:
        try:
            connection.send({"error": repr(exc)})
        except (BrokenPipeError, EOFError):
            pass
    finally:
        if started:
            observer.stop()
        connection.close()


class Observer:
    def __init__(self, pool, groups, out, interval_s=.05, process_interval_s=.2):
        self.pool, self.groups, self.out = pool, groups, out
        self.interval, self.process_interval = interval_s, process_interval_s
        self.io = ProcessIO()
        self.process = self.connection = None
        self.started = False
        self.quality = None
        self.stopped = False

    def receive(self, timeout=20):
        if not self.connection.poll(timeout):
            raise TimeoutError("Independent observer did not respond")
        value = self.connection.recv()
        if "error" in value:
            raise RuntimeError(value["error"])
        return value

    def prepare(self):
        if self.process:
            return
        ctx = multiprocessing.get_context("spawn")
        self.connection, child = ctx.Pipe()
        config = dict(pool=self.pool, groups=self.groups, out=self.out, interval=self.interval,
                      process_interval=self.process_interval, allowed_cpus=getattr(self.io, "allowed_cpus", None),
                      experiment_root=getattr(self, "experiment_root", None),
                      extra_roots=getattr(self, "extra_roots", {}))
        self.process = ctx.Process(target=worker, args=(config, child), daemon=True)
        self.process.start()
        child.close()
        if self.receive() != {"ready": True}:
            raise RuntimeError("Independent observer failed preparation")
        import json
        (self.out/'observer-process.json').write_text(json.dumps(dict(pid=self.process.pid,
            prepared_monotonic_s=time.monotonic(),
            role='observer only; PID must be verified live before diagnostic attachment'))+'\n')

    def start(self):
        self.prepare()
        self.connection.send("start")
        if self.receive() != {"started": True}:
            raise RuntimeError("Independent observer did not start")
        self.started = True

    def stop(self):
        if self.stopped or self.quality is not None:
            return self.quality
        if self.process is None:
            return None
        failure = None
        try:
            if self.started:
                self.connection.send("stop")
                value = self.receive(timeout=35)
                self.quality = value["quality"]
                self.io.affinity_checked_threads = value["affinity_checked_threads"]
                self.io.affinity_violations = value["affinity_violations"]
                self.io.errors = Counter(value["io_errors"])
                self.io.races = value["io_races"]
            else:
                self.connection.send("cancel")
        except (OSError, EOFError, TimeoutError, RuntimeError, KeyError) as exc:
            # A dead sampler invalidates measurement, but must not prevent the
            # caller's finally block from reaping tools and removing cgroups.
            failure = dict(type=type(exc).__name__, message=str(exc))
        finally:
            self.connection.close()
            self.process.join(timeout=5)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=5)
                failure = failure or dict(type='WorkerTermination', message='Observer required termination')
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join(timeout=5)
        if self.process.exitcode != 0 or failure:
            self.quality = dict(valid=False, observer_process_isolated=True,
                observer_worker_exitcode=self.process.exitcode,
                observer_failure=failure or dict(type='WorkerExit', message='Observer worker failed'),
                gap_p95_s=None, gap_max_s=None, observer_thread_cpu_s=None,
                measurement_complete=False)
            (self.out/'observation-quality.json').write_text(json.dumps(self.quality, indent=2)+'\n')
        self.started = False
        self.stopped = True
        return self.quality
