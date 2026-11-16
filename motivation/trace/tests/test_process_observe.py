import ctypes
import json
from pathlib import Path
import tempfile
import time
import unittest

from process_observe import Observer


class IndependentObserverTests(unittest.TestCase):
    def test_parent_gil_pause_does_not_interrupt_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, value in {
                "memory.current": "0", "cpu.stat": "usage_usec 0\n",
                "memory.events": "oom 0\noom_kill 0\n",
                "cpu.pressure": "some avg10=0 avg60=0 avg300=0 total=0\n",
                "memory.pressure": "some avg10=0 avg60=0 avg300=0 total=0\n",
                "io.pressure": "some avg10=0 avg60=0 avg300=0 total=0\n",
            }.items():
                (root / name).write_text(value)
            observer = Observer(root, {}, root)
            observer.prepare()
            observer.start()
            try:
                time.sleep(.25)
                # PyDLL keeps this interpreter's GIL throughout the C call.
                ctypes.PyDLL(None).usleep(400000)
                time.sleep(.25)
            finally:
                quality = observer.stop()
            self.assertTrue(quality["valid"], quality)
            self.assertTrue(quality["observer_process_isolated"])
            self.assertLess(quality["gap_max_s"], .25)
            self.assertGreater(quality["observer_process_cpu_s"], 0)
            self.assertEqual(quality["writer_rows_written"], quality["sampler_rows_enqueued"])
            self.assertFalse(observer.process.is_alive())

    def test_prepared_observer_can_be_cancelled_without_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            observer = Observer(Path(tmp), {}, Path(tmp))
            observer.prepare()
            self.assertIsNone(observer.stop())
            self.assertFalse(observer.process.is_alive())

    def test_dead_worker_invalidates_measurement_and_stop_is_repeatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            observer = Observer(Path(tmp), {}, Path(tmp))
            observer.prepare()
            observer.process.kill()
            observer.process.join(timeout=5)
            # Simulates losing a worker after start, before its final report.
            observer.started = True
            quality = observer.stop()
            self.assertFalse(quality['valid'])
            self.assertEqual(quality['observer_worker_exitcode'], -9)
            self.assertFalse(quality['measurement_complete'])
            self.assertIs(observer.stop(), quality)
            self.assertEqual(json.loads((Path(tmp)/'observation-quality.json').read_text()), quality)
