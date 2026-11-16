from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from async_observe import Observer, TimedCounterPath
from metric_validity import critical_quality


def fake_counters(_):
    return {k: 0 for k in ("memory.current", "cpu.stat", "memory.events",
                           "cpu.pressure", "memory.pressure", "io.pressure")}


class ObservationTests(unittest.TestCase):
    def test_slow_memory_breakdown_cannot_stall_critical_sampling(self):
        original=Path.read_text
        def sampled(path):
            result=fake_counters(path)
            memory_stat=path/'memory.stat'
            result['memory.stat']=memory_stat.read_text() if memory_stat.exists() else None
            return result
        with tempfile.TemporaryDirectory() as tmp, patch('async_observe.counters',sampled), \
                patch('observe.counters',fake_counters),patch('async_observe.host_snapshot',return_value={}):
            root=Path(tmp);(root/'memory.stat').write_text('anon 123\nfile 456\n')
            delayed=False
            def slow(path,*args,**kwargs):
                nonlocal delayed
                if path==root/'memory.stat' and not delayed:
                    delayed=True;time.sleep(1.1)
                return original(path,*args,**kwargs)
            observer=Observer(root,{},root);observer.io.sample=lambda _:{}
            with patch.object(Path,'read_text',slow):
                observer.start();time.sleep(1.5);q=observer.stop()
            self.assertTrue(critical_quality(q),q)
            self.assertFalse(q['valid'])  # The auxiliary loss stays visible.
            self.assertGreater(q['diagnostic_gap_max_s'],1.)
            self.assertTrue(q['slow_memory_breakdown_reads'])
            import json
            rows=[json.loads(line) for line in (root/'resources.jsonl').read_text().splitlines()]
            self.assertTrue(all(row['pool']['memory.stat'] is None for row in rows))
            detail=next(row['memory_breakdown']['pool'] for row in rows if 'memory_breakdown' in row)
            self.assertEqual(detail['values'],{'anon':123,'file':456})
            # The latest diagnostic snapshot may replace an intermediate one;
            # slow-read acquisition evidence must still be retained separately.
            slow_read=q['slow_memory_breakdown_reads'][0]
            self.assertGreater(slow_read['read_end_s']-slow_read['read_start_s'],1.)

    def test_slow_metadata_check_is_reported_separately_from_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'memory.current';path.write_text('123')
            timing={};wrapped=TimedCounterPath(path,timing)
            original=Path.exists
            def slow(p):
                time.sleep(.03)
                return original(p)
            with patch.object(Path,'exists',slow):
                self.assertTrue(wrapped.exists())
            self.assertEqual(wrapped.read_text(),'123')
            self.assertGreaterEqual(timing['memory.current.exists']['wall_s'],.03)
            self.assertIn('thread_cpu_s',timing['memory.current.exists'])
            self.assertIn('memory.current',timing)

    def test_slow_redundant_leaf_counter_does_not_block_aggregate_sampling(self):
        original=Path.read_text
        delayed=False
        with tempfile.TemporaryDirectory() as tmp, patch('async_observe.counters',fake_counters), \
                patch('observe.counters',fake_counters), patch('async_observe.host_snapshot',return_value={}):
            root=Path(tmp);leaf=root/'leaf';leaf.mkdir()
            (leaf/'memory.current').write_text('123')
            (leaf/'cpu.stat').write_text('usage_usec 1\n')
            def slow(path,*args,**kwargs):
                nonlocal delayed
                if path==leaf/'cpu.stat' and not delayed:
                    delayed=True
                    time.sleep(.3)
                return original(path,*args,**kwargs)
            observer=Observer(root,{'agent':leaf},root);observer.io.sample=lambda _:{}
            with patch.object(Path,'read_text',slow):
                observer.start();time.sleep(1.1);q=observer.stop()
            self.assertTrue(q['valid'],q)
            self.assertLess(q['gap_max_s'],.25)
            self.assertTrue(q['slow_detail_counter_reads'])
            self.assertGreater(q['diagnostic_gap_max_s'],.25)

    def test_slow_log_write_does_not_interrupt_sampling_or_drop_rows(self):
        class SlowWriter(Observer):
            def write_line(self, stream, line):
                if self.written == 0:
                    time.sleep(.4)
                super().write_line(stream, line)
        with tempfile.TemporaryDirectory() as tmp, patch("async_observe.counters", fake_counters), \
                patch("observe.counters", fake_counters), patch("async_observe.host_snapshot", return_value={}):
            observer = SlowWriter(Path(tmp), {}, Path(tmp))
            observer.start()
            time.sleep(.8)
            q = observer.stop()
            self.assertTrue(q["valid"], q)
            self.assertGreater(q["writer_max_write_s"], .35)
            self.assertLess(q["gap_max_s"], .25)
            self.assertEqual(q["writer_rows_written"], q["sampler_rows_enqueued"])
            self.assertEqual(q["sample_count"], len((Path(tmp)/"resources.jsonl").read_text().splitlines()))

    def test_slow_counter_read_still_invalidates_measurement(self):
        calls = 0
        def slow(group):
            nonlocal calls
            calls += 1
            if calls == 3:
                time.sleep(.4)
            return fake_counters(group)
        with tempfile.TemporaryDirectory() as tmp, patch("async_observe.counters", slow), \
                patch("observe.counters", fake_counters), patch("async_observe.host_snapshot", return_value={}):
            observer = Observer(Path(tmp), {}, Path(tmp))
            observer.start()
            time.sleep(.9)
            q = observer.stop()
            self.assertFalse(q["valid"])
            self.assertGreater(q["gap_max_s"], .35)

    def test_failed_writer_never_reports_complete_measurements(self):
        failed = threading.Event()
        class FailedWriter(Observer):
            def write_line(self, stream, line):
                failed.set()
                raise OSError("injected disk failure")
        with tempfile.TemporaryDirectory() as tmp, patch("async_observe.counters", fake_counters), \
                patch("observe.counters", fake_counters), patch("async_observe.host_snapshot", return_value={}):
            observer = FailedWriter(Path(tmp), {}, Path(tmp))
            observer.start()
            self.assertTrue(failed.wait(2))
            q = observer.stop()
            self.assertFalse(q["valid"])
            self.assertTrue(q["writer_errors"])
            self.assertNotEqual(q["sampler_rows_enqueued"], q["writer_rows_written"])
