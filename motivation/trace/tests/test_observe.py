import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from observe import Observer, percentile


class ObservationTests(unittest.TestCase):

    def test_missing_critical_sensor_fails_before_launch(self):
        observer = Observer(Path("/missing"), {}, Path("/unused"))
        with patch("observe.counters", return_value={"memory.current": None}):
            with self.assertRaisesRegex(RuntimeError, "Required observation missing"):
                observer.start()

    def test_small_sample_tail_is_not_the_minimum(self):
        self.assertAlmostEqual(percentile([1, 10], .95), 9.55)
        self.assertEqual(percentile([1, 10], .5), 5.5)



if __name__ == "__main__":
    unittest.main()
