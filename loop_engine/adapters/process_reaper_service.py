"""Bounded service loop for periodic orphan process reaping."""

from __future__ import annotations

import threading
import time
import weakref
from dataclasses import asdict, dataclass
from typing import Callable
from uuid import uuid4

from .process_registry import ProcessRecord, ProcessRegistry
from .service_reports import ServiceRunReport, ServiceRunReportSink
from .subprocesses import reap_stale_processes


@dataclass(frozen=True, slots=True)
class ProcessRetentionPolicy:
    retain_seconds: float
    prune_every_cycles: int = 1
    max_pruned_per_cycle: int = 100

    def __post_init__(self) -> None:
        if (
            not isinstance(self.retain_seconds, (int, float))
            or isinstance(self.retain_seconds, bool)
            or self.retain_seconds < 0
        ):
            raise ValueError("retention seconds must not be negative")
        if (
            not isinstance(self.prune_every_cycles, int)
            or isinstance(self.prune_every_cycles, bool)
            or self.prune_every_cycles <= 0
        ):
            raise ValueError("retention prune cadence must be positive")
        if (
            not isinstance(self.max_pruned_per_cycle, int)
            or isinstance(self.max_pruned_per_cycle, bool)
            or self.max_pruned_per_cycle <= 0
        ):
            raise ValueError("retention prune limit must be positive")


@dataclass(frozen=True, slots=True)
class ProcessReaperPolicy:
    stale_after_seconds: float
    interval_seconds: float
    max_cycles: int
    retention: ProcessRetentionPolicy | None = None

    def __post_init__(self) -> None:
        if self.stale_after_seconds <= 0:
            raise ValueError("reaper stale threshold must be positive")
        if self.interval_seconds <= 0:
            raise ValueError("reaper interval must be positive")
        if self.max_cycles <= 0:
            raise ValueError("reaper max_cycles must be positive")
        if self.retention is not None and not isinstance(
            self.retention,
            ProcessRetentionPolicy,
        ):
            raise TypeError("reaper retention must be ProcessRetentionPolicy")


@dataclass(frozen=True, slots=True)
class ReaperCycleReport:
    cycle: int
    started_at: float
    finished_at: float
    reaped_record_ids: tuple[str, ...]
    terminated_count: int
    lost_count: int
    pruning_attempted: bool = False
    pruned_count: int = 0
    pruning_error: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessReaperReport:
    run_id: str
    status: str
    stop_reason: str
    started_at: float
    finished_at: float
    cycles: tuple[ReaperCycleReport, ...]
    error: str | None = None

    @property
    def reaped_count(self) -> int:
        return sum(len(cycle.reaped_record_ids) for cycle in self.cycles)

    def to_service_report(self) -> ServiceRunReport:
        return ServiceRunReport(
            run_id=self.run_id,
            service="process_reaper",
            status=self.status,
            stop_reason=self.stop_reason,
            started_at=self.started_at,
            finished_at=self.finished_at,
            metrics={
                "cycle_count": len(self.cycles),
                "reaped_count": self.reaped_count,
                "terminated_count": sum(
                    cycle.terminated_count for cycle in self.cycles
                ),
                "lost_count": sum(cycle.lost_count for cycle in self.cycles),
                "pruned_count": sum(cycle.pruned_count for cycle in self.cycles),
            },
            details={"cycles": [asdict(cycle) for cycle in self.cycles]},
            error=self.error,
        )


ReapFunction = Callable[[ProcessRegistry, float], list[ProcessRecord]]
PruneFunction = Callable[[ProcessRegistry, float, int], int]
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
        pruner: PruneFunction | None = None,
        clock: Callable[[], float] = time.time,
        report_store: ServiceRunReportSink | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self._reaper = reaper or _default_reaper
        self._pruner = pruner or _default_pruner
        self._clock = clock
        self._report_store = report_store
        self._run_lock = _registry_run_lock(registry)

    def run(
        self,
        *,
        stop_event: threading.Event | None = None,
    ) -> ProcessReaperReport:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("process reaper service is already running")
        event = stop_event or threading.Event()
        run_id = uuid4().hex
        started_at = self._clock()
        cycles: list[ReaperCycleReport] = []
        try:
            if event.is_set():
                return self._report(
                    status="completed",
                    stop_reason="stop_requested",
                    run_id=run_id,
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
                        run_id=run_id,
                        started_at=started_at,
                        cycles=cycles,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                retention = self.policy.retention
                if (
                    retention is not None
                    and cycle_number % retention.prune_every_cycles == 0
                ):
                    try:
                        pruned_count = self._pruner(
                            self.registry,
                            retention.retain_seconds,
                            retention.max_pruned_per_cycle,
                        )
                        if (
                            not isinstance(pruned_count, int)
                            or isinstance(pruned_count, bool)
                            or pruned_count < 0
                        ):
                            raise TypeError(
                                "process pruner must return a non-negative integer"
                            )
                        cycle_report = _with_pruning(
                            cycle_report,
                            pruned_count=pruned_count,
                        )
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                        cycles.append(
                            _with_pruning(
                                cycle_report,
                                pruning_error=error,
                            )
                        )
                        return self._report(
                            status="failed",
                            stop_reason="error",
                            run_id=run_id,
                            started_at=started_at,
                            cycles=cycles,
                            error=f"process_retention_error:{error}",
                        )
                cycles.append(cycle_report)
                if cycle_number == self.policy.max_cycles:
                    return self._report(
                        status="completed",
                        stop_reason="max_cycles",
                        run_id=run_id,
                        started_at=started_at,
                        cycles=cycles,
                    )
                if event.wait(self.policy.interval_seconds):
                    return self._report(
                        status="completed",
                        stop_reason="stop_requested",
                        run_id=run_id,
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
        run_id: str,
        started_at: float,
        cycles: list[ReaperCycleReport],
        error: str | None = None,
    ) -> ProcessReaperReport:
        report = ProcessReaperReport(
            run_id=run_id,
            status=status,
            stop_reason=stop_reason,
            started_at=started_at,
            finished_at=self._clock(),
            cycles=tuple(cycles),
            error=error,
        )
        if self._report_store is None:
            return report
        try:
            self._report_store.save(report.to_service_report())
        except Exception as exc:
            return ProcessReaperReport(
                run_id=run_id,
                status="failed",
                stop_reason="error",
                started_at=started_at,
                finished_at=self._clock(),
                cycles=tuple(cycles),
                error=(
                    "service_report_persistence_error:"
                    f"{type(exc).__name__}: {exc}"
                ),
            )
        return report


def _default_reaper(
    registry: ProcessRegistry,
    stale_after_seconds: float,
) -> list[ProcessRecord]:
    return reap_stale_processes(
        registry,
        stale_after_seconds=stale_after_seconds,
    )


def _default_pruner(
    registry: ProcessRegistry,
    retain_seconds: float,
    max_records: int,
) -> int:
    return registry.prune_terminal(
        retain_seconds=retain_seconds,
        max_records=max_records,
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


def _with_pruning(
    report: ReaperCycleReport,
    *,
    pruned_count: int = 0,
    pruning_error: str | None = None,
) -> ReaperCycleReport:
    return ReaperCycleReport(
        cycle=report.cycle,
        started_at=report.started_at,
        finished_at=report.finished_at,
        reaped_record_ids=report.reaped_record_ids,
        terminated_count=report.terminated_count,
        lost_count=report.lost_count,
        pruning_attempted=True,
        pruned_count=pruned_count,
        pruning_error=pruning_error,
    )
