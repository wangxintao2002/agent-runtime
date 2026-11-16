"""Keep counter sampling independent of slow diagnostics and log-file writes.

Same critical sample-gap limits as the original observer. A bounded writer queue
must drain completely; it never drops measurements to manufacture a valid run.
"""
import json
import queue
import threading
import time

from observe import Observer as OriginalObserver, host_snapshot, percentile
from counters import counters


class TimedCounterPath:
    """Trace slow cgroup reads without changing counters or sampling limits."""
    def __init__(self, path, timings, skip_memory_stat=False):
        self.path, self.timings = path, timings
        self.skip_memory_stat=skip_memory_stat

    def __truediv__(self, name):
        return TimedCounterPath(self.path / name, self.timings, self.skip_memory_stat)

    def exists(self):
        if self.skip_memory_stat and self.path.name=='memory.stat':
            return False
        start, cpu = time.monotonic(), time.thread_time()
        try:
            return self.path.exists()
        finally:
            self.timings[self.path.name + '.exists'] = dict(
                wall_s=time.monotonic()-start, thread_cpu_s=time.thread_time()-cpu)

    def read_text(self):
        start, cpu = time.monotonic(), time.thread_time()
        try:
            return self.path.read_text()
        finally:
            wall = time.monotonic() - start
            self.timings[self.path.name] = dict(wall_s=wall, thread_cpu_s=time.thread_time()-cpu)


class Observer(OriginalObserver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = queue.Queue(maxsize=4096)
        self.latest_diagnostic = None
        self.diagnostic_times = []
        self.diagnostic_errors = []
        self.writer_errors = []
        self.writer_max_s = 0
        self.queue_peak = self.written = self.enqueued = 0
        self.writer_cpu_s = self.diagnostic_cpu_s = 0
        self.slow_counter_reads = []
        self.slow_detail_reads = []
        self.slow_counter_sections = []
        self.slow_writes = []
        self.slow_memory_breakdown_reads = []
        self.writer = threading.Thread(target=self.write_records, daemon=True)
        self.diagnostic = threading.Thread(target=self.diagnose, daemon=True)

    def start(self):
        super().start()
        self.writer.start()
        self.diagnostic.start()

    def write_line(self, stream, line):
        stream.write(line)

    def write_records(self):
        cpu_start = time.thread_time()
        try:
            with (self.out / "resources.jsonl").open("w", buffering=1) as stream:
                while True:
                    record = self.records.get()
                    if record is None:
                        break
                    before = time.monotonic()
                    cpu_before = time.thread_time()
                    self.write_line(stream, record)
                    wall = time.monotonic()-before
                    self.writer_max_s = max(self.writer_max_s, wall)
                    if wall >= .01:
                        self.slow_writes.append(dict(monotonic_s=before, wall_s=wall,
                            thread_cpu_s=time.thread_time()-cpu_before))
                    self.written += 1
        except Exception as exc:
            self.writer_errors.append(repr(exc))
            self.stop_event.set()
        self.writer_cpu_s = time.thread_time()-cpu_start

    def diagnose(self):
        cpu_start = time.thread_time()
        while not self.stop_event.is_set():
            before = time.monotonic()
            try:
                data = {"processes": self.io.sample(self.groups.values()), "host": host_snapshot(),
                        "diagnostics_monotonic_s": before}
                # The aggregate pool already contains all task descendants.
                # Redundant leaf counters are diagnostics, not the critical
                # aggregate sampling path; a slow leaf read must not stall it.
                details={}
                for name,group in self.groups.items():
                    timings={};path=TimedCounterPath(group,timings)
                    details[name]={"memory.current":int((path/'memory.current').read_text()),
                                   "cpu.stat":(path/'cpu.stat').read_text()}
                    slow={key:value for key,value in timings.items() if value['wall_s']>=.01}
                    if slow:self.slow_detail_reads.append(dict(monotonic_s=before,task=name,reads=slow))
                data['tasks']=details
                data['task_counters_monotonic_s']=before
                # memory.stat can block for seconds even while memory.current
                # and cpu.stat remain responsive. Read category breakdowns only
                # here, with their own acquisition interval; never backfill them
                # into fast samples as if they were fresh 50 ms observations.
                roots={'pool':self.pool}
                if getattr(self,'experiment_root',None):roots['experiment']=self.experiment_root
                roots.update({'service:'+name:path for name,path in getattr(self,'extra_roots',{}).items()})
                breakdown={}
                for scope,root in roots.items():
                    timing={};path=TimedCounterPath(root,timing)/'memory.stat'
                    read_start=time.monotonic()
                    value=({key:int(value) for key,value in (line.split() for line in path.read_text().splitlines())}
                           if path.exists() else None)
                    read_end=time.monotonic()
                    breakdown[scope]=dict(values=value,read_start_s=read_start,read_end_s=read_end)
                    if read_end-read_start>=.01:
                        self.slow_memory_breakdown_reads.append(dict(scope=scope,read_start_s=read_start,
                            read_end_s=read_end,operations=timing))
                data['memory_breakdown']=breakdown
                data["diagnostics_read_cost_s"] = time.monotonic()-before
                self.latest_diagnostic = (len(self.diagnostic_times), data)
                self.diagnostic_times.append(before)
            except Exception as exc:
                self.diagnostic_errors.append({"monotonic_s": before, "error": repr(exc)})
            self.stop_event.wait(max(0, before+self.process_interval-time.monotonic()))
        self.diagnostic_cpu_s = time.thread_time()-cpu_start

    def run(self):
        cpu_start = time.thread_time()
        next_due, last_diagnostic = time.monotonic(), -1
        while not self.stop_event.is_set():
            before = time.monotonic()
            try:
                read_timings = {}
                counter_start, counter_cpu = time.monotonic(), time.thread_time()
                row = {"monotonic_s": before, "pool": counters(TimedCounterPath(self.pool, read_timings,skip_memory_stat=True))}
                counter_wall = time.monotonic()-counter_start
                if counter_wall >= .01:
                    self.slow_counter_sections.append(dict(monotonic_s=counter_start, scope='pool',
                        wall_s=counter_wall, thread_cpu_s=time.thread_time()-counter_cpu,
                        path_operation_wall_s=sum(v['wall_s'] for v in read_timings.values()),
                        operations=read_timings))
                slow = {name: values for name, values in read_timings.items() if values['wall_s'] >= .01}
                if slow:
                    row['slow_pool_counter_reads'] = slow
                    self.slow_counter_reads.append(dict(monotonic_s=before, reads=slow))
                if getattr(self, "experiment_root", None):
                    row["experiment"] = counters(TimedCounterPath(self.experiment_root,{},skip_memory_stat=True))
                if getattr(self, "extra_roots", None):
                    row['services']={name:counters(TimedCounterPath(path,{},skip_memory_stat=True)) for name,path in self.extra_roots.items()}
                row["tasks"] = {}
                row["read_cost_s"] = time.monotonic()-before
                diagnostic = self.latest_diagnostic
                if diagnostic is not None and diagnostic[0] != last_diagnostic:
                    last_diagnostic = diagnostic[0]
                    row.update(diagnostic[1])
                self.records.put_nowait(json.dumps(row)+"\n")
                self.enqueued += 1
                self.queue_peak = max(self.queue_peak, self.records.qsize())
                self.timestamps.append(before)
                self.costs.append(time.monotonic()-before)
            except Exception as exc:
                self.errors.append({"monotonic_s": before, "error": repr(exc)})
                if isinstance(exc, queue.Full):
                    self.stop_event.set()
            next_due += self.interval
            now = time.monotonic()
            if next_due < now:
                next_due = now
            self.stop_event.wait(max(0, next_due-now))
        self.cpu_s = time.thread_time()-cpu_start

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=10)
        self.diagnostic.join(timeout=10)
        if self.thread.is_alive() or self.diagnostic.is_alive():
            raise RuntimeError("Sampler/diagnostics failed to stop")
        if self.writer.is_alive():
            self.records.put(None, timeout=10)
            self.writer.join(timeout=10)
        if self.writer.is_alive():
            raise RuntimeError("Measurement writer failed to drain")
        gaps = [b-a for a,b in zip(self.timestamps, self.timestamps[1:])]
        dgaps = [b-a for a,b in zip(self.diagnostic_times, self.diagnostic_times[1:])]
        valid = (not self.errors and not self.writer_errors and not self.diagnostic_errors
            and self.enqueued == self.written and len(gaps)>2
            and percentile(gaps,.95) <= self.interval*2 and max(gaps,default=0) <= self.interval*5
            and len(dgaps)>1 and percentile(dgaps,.95) <= self.process_interval*2
            and max(dgaps,default=0) <= self.process_interval*5)
        quality = dict(sample_count=len(self.timestamps), requested_interval_s=self.interval,
            gap_p50_s=percentile(gaps,.5), gap_p95_s=percentile(gaps,.95), gap_max_s=max(gaps,default=0),
            read_cost_p95_s=percentile(self.costs,.95), read_cost_max_s=max(self.costs,default=0),
            observer_thread_cpu_s=self.cpu_s, writer_cpu_s=self.writer_cpu_s,
            slow_pool_counter_reads=self.slow_counter_reads,
            slow_detail_counter_reads=self.slow_detail_reads,
            slow_counter_sections=self.slow_counter_sections,
            slow_log_writes=self.slow_writes,
            counter_path_operations_timed=['exists','read_text'],
            counter_protocol='critical-50ms_memory-breakdown-and-process-diagnostics-200ms-v3',
            leaf_counters_are_diagnostic=True,
            memory_breakdown_is_diagnostic=True,
            slow_memory_breakdown_reads=self.slow_memory_breakdown_reads,
            diagnostic_cpu_s=self.diagnostic_cpu_s, diagnostic_gap_max_s=max(dgaps,default=0), diagnostic_gap_p95_s=percentile(dgaps,.95),
            diagnostic_samples=len(self.diagnostic_times), diagnostic_errors=self.diagnostic_errors,
            writer_errors=self.writer_errors, writer_max_write_s=self.writer_max_s,
            writer_queue_peak=self.queue_peak, writer_rows_written=self.written,
            sampler_rows_enqueued=self.enqueued, errors=self.errors,
            process_io_errors=dict(self.io.errors), process_io_exit_races=self.io.races,
            process_io_is_diagnostic_snapshot_not_sum=True,
            observer_mode="separate counter sampler, diagnostics, bounded lossless writer", valid=valid)
        (self.out / "observation-quality.json").write_text(json.dumps(quality,indent=2)+"\n")
        return quality
