"""Bounded service loop for periodic orphan process reaping."""

from __future__ import annotations

import threading
import time
import weakref
from dataclasses import dataclass
from typing import Callable

from .process_registry import ProcessRecord, ProcessRegistry
from .subprocesses import reap_stale_processes


@dataclass(frozen=True, slots=True)
class ProcessReaperPolicy:
    stale_after_seconds: float
    interval_seconds: float
    max_cycles: int

    def __post_init__(self) -> None:
        if self.stale_after_seconds <= 0:
            raise ValueError("reaper stale threshold must be positive")
        if self.interval_seconds <= 0:
            raise ValueError("reaper interval must be positive")
        if self.max_cycles <= 0:
            raise ValueError("reaper max_cycles must be positive")


@dataclass(frozen=True, slots=True)
class ReaperCycleReport:
    cycle: int
    started_at: float
    finished_at: float
    reaped_record_ids: tuple[str, ...]
    terminated_count: int
    lost_count: int


@dataclass(frozen=True, slots=True)
class ProcessReaperReport:
    status: str
    stop_reason: str
    started_at: float
    finished_at: float
    cycles: tuple[ReaperCycleReport, ...]
    error: str | None = None

    @property
    def reaped_count(self) -> int:
        return sum(len(cycle.reaped_record_ids) for cycle in self.cycles)


ReapFunction = Callable[[ProcessRegistry, float], list[ProcessRecord]]
_REGISTRY_LOCKS_GUARD = threading.Lock()
_REGISTRY_LOCKS: weakref.WeakKeyDictionary[
    ProcessRegistry,
    threading.Lock,
] = weakref.WeakKeyDictionary()


class ProcessReaperService:
    """Run identity-safe stale-process sweeps under explicit lifecycle bounds."""

    def __init__(
        self,
        registry: ProcessRegistry,
        policy: ProcessReaperPolicy,
        *,
        reaper: ReapFunction | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self._reaper = reaper or _default_reaper
        self._clock = clock
        self._run_lock = _registry_run_lock(registry)

    def run(
        self,
        *,
        stop_event: threading.Event | None = None,
    ) -> ProcessReaperReport:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("process reaper service is already running")
        event = stop_event or threading.Event()
        started_at = self._clock()
        cycles: list[ReaperCycleReport] = []
        try:
            if event.is_set():
                return self._report(
                    status="completed",
                    stop_reason="stop_requested",
                    started_at=started_at,
                    cycles=cycles,
                )
            for cycle_number in range(1, self.policy.max_cycles + 1):
                cycle_started = self._clock()
                try:
                    reaped = self._reaper(
                        self.registry,
                        self.policy.stale_after_seconds,
                    )
                    cycle_report = _cycle_report(
                        cycle_number,
                        cycle_started,
                        self._clock(),
                        reaped,
                    )
                except Exception as exc:
                    return self._report(
                        status="failed",
                        stop_reason="error",
                        started_at=started_at,
                        cycles=cycles,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                cycles.append(cycle_report)
                if cycle_number == self.policy.max_cycles:
                    return self._report(
                        status="completed",
                        stop_reason="max_cycles",
                        started_at=started_at,
                        cycles=cycles,
                    )
                if event.wait(self.policy.interval_seconds):
                    return self._report(
                        status="completed",
                        stop_reason="stop_requested",
                        started_at=started_at,
                        cycles=cycles,
                    )
            raise AssertionError("bounded reaper loop did not terminate")
        finally:
            self._run_lock.release()

    def _report(
        self,
        *,
        status: str,
        stop_reason: str,
        started_at: float,
        cycles: list[ReaperCycleReport],
        error: str | None = None,
    ) -> ProcessReaperReport:
        return ProcessReaperReport(
            status=status,
            stop_reason=stop_reason,
            started_at=started_at,
            finished_at=self._clock(),
            cycles=tuple(cycles),
            error=error,
        )


def _default_reaper(
    registry: ProcessRegistry,
    stale_after_seconds: float,
) -> list[ProcessRecord]:
    return reap_stale_processes(
        registry,
        stale_after_seconds=stale_after_seconds,
    )


def _registry_run_lock(registry: ProcessRegistry) -> threading.Lock:
    with _REGISTRY_LOCKS_GUARD:
        lock = _REGISTRY_LOCKS.get(registry)
        if lock is None:
            lock = threading.Lock()
            _REGISTRY_LOCKS[registry] = lock
        return lock


def _cycle_report(
    cycle: int,
    started_at: float,
    finished_at: float,
    records: list[ProcessRecord],
) -> ReaperCycleReport:
    if not isinstance(records, list) or any(
        not isinstance(record, ProcessRecord) for record in records
    ):
        raise TypeError("process reaper must return a list of ProcessRecord")
    return ReaperCycleReport(
        cycle=cycle,
        started_at=started_at,
        finished_at=finished_at,
        reaped_record_ids=tuple(record.record_id for record in records),
        terminated_count=sum(record.status == "terminated" for record in records),
        lost_count=sum(record.status == "lost" for record in records),
    )
