"""
Unit tests for safe_io.py — atomic_write_json + file_lock.
Run with: python3 -m unittest tests.test_safe_io
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from safe_io import atomic_write_json, file_lock, safe_read_json


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "test.json"

    def test_roundtrip(self):
        atomic_write_json(self.path, {"a": 1, "b": [2, 3]})
        self.assertEqual(safe_read_json(self.path), {"a": 1, "b": [2, 3]})

    def test_overwrite_atomic(self):
        atomic_write_json(self.path, {"v": "first"})
        atomic_write_json(self.path, {"v": "second"})
        self.assertEqual(safe_read_json(self.path)["v"], "second")

    def test_tmp_cleanup(self):
        atomic_write_json(self.path, {"x": 1})
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        self.assertFalse(tmp_path.exists(), "temp file should be removed after rename")

    def test_corrupted_file_fallback(self):
        self.path.write_text("not valid json{")
        result = safe_read_json(self.path, default={"recovered": True})
        self.assertEqual(result, {"recovered": True})

    def test_missing_file_default(self):
        # Path doesn't exist
        nonexistent = self.tmp / "does_not_exist.json"
        self.assertEqual(safe_read_json(nonexistent, default=[]), [])


class FileLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "counter.json"
        atomic_write_json(self.path, {"counter": 0})

    def test_concurrent_increments_no_lost_updates(self):
        """20 threads each do load -> +1 -> save; final must be 20, not less."""

        def increment():
            with file_lock(self.path):
                d = safe_read_json(self.path, {})
                d["counter"] = d.get("counter", 0) + 1
                time.sleep(0.01)  # widen race window
                atomic_write_json(self.path, d)

        threads = [threading.Thread(target=increment) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(safe_read_json(self.path)["counter"], 20)

    def test_lock_timeout(self):
        """A second lock acquire while one is held must time out."""
        held = threading.Event()
        released = threading.Event()

        def hold():
            with file_lock(self.path):
                held.set()
                released.wait(timeout=2)

        t = threading.Thread(target=hold)
        t.start()
        held.wait(timeout=2)

        with self.assertRaises(TimeoutError):
            with file_lock(self.path, timeout=0.1):
                self.fail("should have timed out")

        released.set()
        t.join()


if __name__ == "__main__":
    unittest.main()
