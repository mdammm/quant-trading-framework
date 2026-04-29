#!/usr/bin/env python3
"""
Test runner for piper_public. Stdlib unittest only — no pytest dep.

Usage:
    python3 tests/run.py
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def main():
    print("=" * 60)
    print("  Piper Public — Test Suite")
    print("=" * 60)

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(Path(__file__).parent), pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
