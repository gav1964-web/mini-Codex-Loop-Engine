"""Stateful fixture for contention and interrupted-task recovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
import time


@dataclass(frozen=True, slots=True)
class RecoveryAudit:
    strategy: str
    verified: bool
    interruption_count: int
    recovery_markers: int
    completed_leaf_reexecutions: int
    independent_overlap: bool
    conflicting_write_overlap: bool


class RecoveryWorkspace:
    def __init__(
        self,
        root: Path,
        *,
        strategy: str,
        operation_delay_seconds: float,
    ) -> None:
        self.root = root
        self.strategy = strategy
        self.operation_delay_seconds = operation_delay_seconds
        self.execution_counts: dict[str, int] = {}
        self.interruption_count = 0
        self._interrupted = False
        self._intervals: dict[str, list[tuple[float, float]]] = {}
        self._active: set[str] = set()
        self._overlaps: set[frozenset[str]] = set()
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.target = self.root / "state.txt"
        self.target.write_text("", encoding="utf-8")

    def inspect(self) -> dict:
        return self._timed_operation(
            "inspect",
            lambda: {"initially_empty": self.target.read_text() == ""},
        )

    def write_a(self) -> dict:
        return self._timed_operation("write_a", lambda: self._append("A"))

    def write_b_with_interruption(self) -> dict:
        self._count("write_b")
        if not self._interrupted:
            self._interrupted = True
            self.interruption_count += 1
            raise SystemExit("simulated_process_interruption")
        return self._timed_operation(
            "write_b",
            lambda: self._append("B"),
            count=False,
        )

    def full_with_interruption(self) -> dict:
        self._count("full")
        if not self._interrupted:
            self._interrupted = True
            self.interruption_count += 1
            raise SystemExit("simulated_process_interruption")
        self.inspect()
        self.write_a()
        self._append("B")
        return self.verify()

    def verify(self) -> dict:
        content = self.target.read_text(encoding="utf-8")
        return {"passed": content == "AB", "content": content}

    def audit(self, graph, *, verified: bool) -> RecoveryAudit:
        recovery_markers = sum(
            node.error == "recovered_after_interrupted_leaf_execution"
            for node in graph.nodes.values()
        )
        completed_reexecutions = sum(
            max(0, count - 1)
            for name, count in self.execution_counts.items()
            if name not in {"write_b", "full"}
        )
        return RecoveryAudit(
            strategy=self.strategy,
            verified=verified,
            interruption_count=self.interruption_count,
            recovery_markers=recovery_markers,
            completed_leaf_reexecutions=completed_reexecutions,
            independent_overlap=(
                frozenset({"inspect", "write_a"}) in self._overlaps
            ),
            conflicting_write_overlap=(
                frozenset({"write_a", "write_b"}) in self._overlaps
            ),
        )

    def _timed_operation(self, name: str, operation, *, count: bool = True):
        if count:
            self._count(name)
        started = time.perf_counter()
        with self._lock:
            self._overlaps.update(
                frozenset({name, active}) for active in self._active
            )
            self._active.add(name)
        try:
            time.sleep(self.operation_delay_seconds)
            return operation()
        finally:
            finished = time.perf_counter()
            with self._lock:
                self._active.remove(name)
                self._intervals.setdefault(name, []).append(
                    (started, finished)
                )

    def _append(self, value: str) -> dict:
        content = self.target.read_text(encoding="utf-8")
        self.target.write_text(content + value, encoding="utf-8")
        return {"written": value}

    def _count(self, name: str) -> None:
        self.execution_counts[name] = self.execution_counts.get(name, 0) + 1
