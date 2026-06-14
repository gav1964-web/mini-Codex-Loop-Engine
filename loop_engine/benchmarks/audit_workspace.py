"""Read-only real-file fixture for the project audit benchmark."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
import time


@dataclass(frozen=True, slots=True)
class ProjectAudit:
    strategy: str
    verified: bool
    acquired_capabilities: tuple[str, ...]
    independent_reads_overlapped: bool


class ProjectAuditWorkspace:
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
        self._intervals: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._create_fixture()

    def inspect_source(self) -> dict:
        content = self._timed_read("source", self.root / "app.py")
        return {
            "file": "app.py",
            "has_normalize": "def normalize_name(" in content,
        }

    def inspect_docs(self) -> dict:
        content = self._timed_read("docs", self.root / "README.md")
        return {
            "file": "README.md",
            "documents_check_command": "python -m unittest" in content,
        }

    def inspect_config(self) -> dict:
        content = self._timed_read("config", self.root / "pyproject.toml")
        return {
            "file": "pyproject.toml",
            "requires_python_311": 'requires-python = ">=3.11"' in content,
        }

    def verify(self) -> dict:
        evidence = {
            **self.inspect_source(),
            **self.inspect_docs(),
            **self.inspect_config(),
        }
        passed = all(
            (
                evidence["has_normalize"],
                evidence["documents_check_command"],
                evidence["requires_python_311"],
            )
        )
        return {"passed": passed, **evidence}

    def acquire(self, capability: str) -> bool:
        if capability != "project.audit.docs":
            return False
        self.acquired_capabilities.append(capability)
        return True

    def audit(self, *, verified: bool) -> ProjectAudit:
        return ProjectAudit(
            strategy=self.strategy,
            verified=verified,
            acquired_capabilities=tuple(sorted(self.acquired_capabilities)),
            independent_reads_overlapped=self._reads_overlapped(),
        )

    def _timed_read(self, name: str, path: Path) -> str:
        started = time.perf_counter()
        content = path.read_text(encoding="utf-8")
        time.sleep(self.read_delay_seconds)
        finished = time.perf_counter()
        with self._lock:
            self._intervals[name] = (started, finished)
        return content

    def _reads_overlapped(self) -> bool:
        intervals = tuple(self._intervals.values())
        if len(intervals) < 3:
            return False
        latest_start = max(interval[0] for interval in intervals)
        earliest_finish = min(interval[1] for interval in intervals)
        return latest_start < earliest_finish

    def _create_fixture(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "app.py").write_text(
            "def normalize_name(value):\n"
            "    return value.strip().casefold()\n",
            encoding="utf-8",
        )
        (self.root / "README.md").write_text(
            "# Sample project\n\n"
            "Check the project with `python -m unittest`.\n",
            encoding="utf-8",
        )
        (self.root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "sample-project"\n'
            'requires-python = ">=3.11"\n',
            encoding="utf-8",
        )
