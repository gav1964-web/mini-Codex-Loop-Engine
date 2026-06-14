"""Fixture for bounded retries with one idempotent side effect."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
import time


@dataclass(frozen=True, slots=True)
class RetryAudit:
    strategy: str
    verified: bool
    transient_failures: int
    side_effect_count: int
    idempotency_keys: tuple[str, ...]
    independent_overlap: bool


class RetryWorkspace:
    def __init__(
        self,
        root: Path,
        *,
        strategy: str,
        operation_delay_seconds: float,
        idempotency_key: str,
    ) -> None:
        self.root = root
        self.strategy = strategy
        self.operation_delay_seconds = operation_delay_seconds
        self.idempotency_key = idempotency_key
        self.transient_failures = 0
        self.side_effect_count = 0
        self.keys: list[str] = []
        self._failed_once = False
        self._active: set[str] = set()
        self._overlaps: set[frozenset[str]] = set()
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.target = self.root / "result.txt"
        self.target.write_text("", encoding="utf-8")

    def inspect(self) -> dict:
        return self._timed("inspect", lambda: {"inspected": True})

    def prepare(self) -> dict:
        return self._timed("prepare", lambda: {"prepared": True})

    def commit(self) -> tuple[bool, dict]:
        self.keys.append(self.idempotency_key)
        if not self._failed_once:
            self._failed_once = True
            self.transient_failures += 1
            return False, {"failure": "transient_io"}

        def apply():
            if self.target.read_text(encoding="utf-8") != "committed":
                self.target.write_text("committed", encoding="utf-8")
                self.side_effect_count += 1
            return {"committed": True}

        return True, self._timed("commit", apply)

    def full(self) -> tuple[bool, dict]:
        success, evidence = self.commit()
        if not success:
            return success, evidence
        self.inspect()
        self.prepare()
        return True, self.verify()

    def verify(self) -> dict:
        return {
            "passed": self.target.read_text(encoding="utf-8") == "committed",
            "side_effect_count": self.side_effect_count,
        }

    def audit(self, *, verified: bool) -> RetryAudit:
        return RetryAudit(
            strategy=self.strategy,
            verified=verified,
            transient_failures=self.transient_failures,
            side_effect_count=self.side_effect_count,
            idempotency_keys=tuple(self.keys),
            independent_overlap=(
                frozenset({"inspect", "prepare"}) in self._overlaps
            ),
        )

    def _timed(self, name: str, operation):
        with self._lock:
            self._overlaps.update(
                frozenset({name, active}) for active in self._active
            )
            self._active.add(name)
        try:
            time.sleep(self.operation_delay_seconds)
            return operation()
        finally:
            with self._lock:
                self._active.remove(name)
