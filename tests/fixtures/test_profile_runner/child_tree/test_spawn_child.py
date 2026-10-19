import subprocess
import sys
import time
import unittest
from pathlib import Path


class SyntheticChildTreeTest(unittest.TestCase):
    def test_spawn_ordinary_child_and_wait(self):
        child = Path(__file__).with_name("child_marker.py")
        subprocess.Popen(
            [sys.executable, str(child)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        deadline = time.monotonic() + 2
        marker = Path(__file__).with_name("child.started")
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(30)
