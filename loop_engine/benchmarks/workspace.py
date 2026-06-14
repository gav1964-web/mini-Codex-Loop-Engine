"""Isolated real-file workspace used by the consolidation benchmark."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import threading
import time


@dataclass(frozen=True, slots=True)
class BenchmarkAudit:
    strategy: str
    verification_passed: bool
    acquired_capabilities: tuple[str, ...]
    independent_reads_overlapped: bool


class PythonChangeWorkspace:
    def __init__(
        self,
        root: Path,
        *,
        strategy: str,
        read_delay_seconds: float,
    ) -> None:
        self.root = root
        self.strategy = strategy
        self.read_delay_seconds = read_delay_seconds
        self.acquired_capabilities: list[str] = []
        self._read_intervals: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._create_fixture()

    def inspect_source(self) -> dict:
        return self._timed_read("source", self.root / "calculator.py")

    def inspect_tests(self) -> dict:
        return self._timed_read("tests", self.root / "test_calculator.py")

    def apply_change(self) -> dict:
        target = self.root / "calculator.py"
        source = target.read_text(encoding="utf-8")
        addition = (
            "\n\ndef mean(values):\n"
            "    values = list(values)\n"
            "    if not values:\n"
            "        raise ValueError(\"mean requires at least one value\")\n"
            "    return total(values) / len(values)\n"
        )
        if "def mean(" not in source:
            target.write_text(source.rstrip() + addition, encoding="utf-8")
        return {"changed": "calculator.py", "mean_present": True}

    def verify(self) -> dict:
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                ".",
                "-p",
                "test_*.py",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return {
            "passed": process.returncode == 0,
            "exit_code": process.returncode,
            "test_count": 2,
        }

    def acquire(self, capability: str) -> bool:
        if capability != "project.inspect.tests":
            return False
        self.acquired_capabilities.append(capability)
        return True

    def audit(self, *, verification_passed: bool) -> BenchmarkAudit:
        return BenchmarkAudit(
            strategy=self.strategy,
            verification_passed=verification_passed,
            acquired_capabilities=tuple(sorted(self.acquired_capabilities)),
            independent_reads_overlapped=self._reads_overlapped(),
        )

    def _timed_read(self, name: str, path: Path) -> dict:
        started = time.perf_counter()
        content = path.read_text(encoding="utf-8")
        time.sleep(self.read_delay_seconds)
        finished = time.perf_counter()
        with self._lock:
            self._read_intervals[name] = (started, finished)
        return {
            "file": path.name,
            "line_count": len(content.splitlines()),
        }

    def _reads_overlapped(self) -> bool:
        source = self._read_intervals.get("source")
        tests = self._read_intervals.get("tests")
        if source is None or tests is None:
            return False
        return max(source[0], tests[0]) < min(source[1], tests[1])

    def _create_fixture(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "calculator.py").write_text(
            "def total(values):\n"
            "    return sum(values)\n",
            encoding="utf-8",
        )
        (self.root / "test_calculator.py").write_text(
            "import unittest\n\n"
            "from calculator import mean, total\n\n\n"
            "class CalculatorTests(unittest.TestCase):\n"
            "    def test_total(self):\n"
            "        self.assertEqual(total([1, 2, 3]), 6)\n\n"
            "    def test_mean(self):\n"
            "        self.assertEqual(mean([2, 4]), 3)\n\n\n"
            "if __name__ == \"__main__\":\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )
