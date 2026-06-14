"""Real multi-process lease contention benchmark with retry jitter."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import multiprocessing
import os
from pathlib import Path
import time

from ..adapters import FileResourceLeaseManager, FileResourceLeasePolicy
from ..tasks import (
    CancellableRetryWaiter,
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    LeafExecutionResult,
    ResourceClaim,
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskRetryPolicy,
    TaskScheduler,
    TaskSchedulerPolicy,
    aggregate_retry_telemetry,
)
from .models import BenchmarkAcceptanceCheck

MULTIPROCESS_CONTENTION_SCHEMA_VERSION = 1
_CAPABILITY = "contention.write"
_RETRY_CODE = "resource_lease_contention"


@dataclass(frozen=True, slots=True)
class ContentionWorkerResult:
    worker: str
    status: str
    started_at: float | None
    finished_at: float | None
    planned_jitter_seconds: float
    telemetry: dict
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MultiprocessContentionReport:
    workers: tuple[ContentionWorkerResult, ...]
    checks: tuple[BenchmarkAcceptanceCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_dict(self) -> dict:
        return {
            "schema_version": MULTIPROCESS_CONTENTION_SCHEMA_VERSION,
            "benchmark": "multiprocess-lease-contention",
            "passed": self.passed,
            "checks": [asdict(check) for check in self.checks],
            "workers": [asdict(worker) for worker in self.workers],
        }

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)


def run_multiprocess_contention_benchmark(
    output_path: str | Path = (
        "build/multiprocess_contention/report.json"
    ),
    *,
    operation_delay_seconds: float = 0.12,
    timeout_seconds: float = 10.0,
) -> MultiprocessContentionReport:
    if operation_delay_seconds <= 0 or timeout_seconds <= 0:
        raise ValueError("benchmark timing bounds must be positive")
    output = Path(output_path).resolve()
    root = output.parent / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    registry = root / "leases.json"
    shared = root / "shared.txt"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    lease_held = context.Event()
    ready = context.Queue()
    results = context.Queue()
    processes = [
        context.Process(
            target=_contention_worker,
            args=(
                worker,
                registry,
                shared,
                operation_delay_seconds,
                start,
                lease_held,
                ready,
                results,
            ),
            name=f"contention-{worker}",
        )
        for worker in ("scheduler-a", "scheduler-b")
    ]
    for process in processes:
        process.start()
    deadline = time.monotonic() + timeout_seconds
    try:
        for _ in processes:
            ready.get(timeout=_remaining(deadline))
        start.set()
        worker_results = tuple(
            sorted(
                (
                    ContentionWorkerResult(**results.get(
                        timeout=_remaining(deadline)
                    ))
                    for _ in processes
                ),
                key=lambda item: item.worker,
            )
        )
    finally:
        start.set()
        for process in processes:
            process.join(timeout=max(0.0, _remaining(deadline)))
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
    report = MultiprocessContentionReport(
        workers=worker_results,
        checks=_checks(worker_results, processes),
    )
    report.save(output)
    return report


def _contention_worker(
    worker: str,
    registry: Path,
    shared: Path,
    operation_delay_seconds: float,
    start,
    lease_held,
    ready,
    results,
) -> None:
    started_at = finished_at = None
    try:
        graph = TaskGraph.create(
            f"Contended write by {worker}",
            graph_id=f"contention-{worker}",
            required_capabilities=[_CAPABILITY],
        )
        retry_policy = _retry_policy(worker)
        planned = retry_policy.decide(
            graph.root,
            _contention_result(worker),
            graph_id=graph.id,
            now=time.time(),
        ).jitter_seconds
        manager = FileResourceLeaseManager(
            registry,
            policy=FileResourceLeasePolicy(
                acquire_timeout_seconds=0.05,
                poll_interval_seconds=0.002,
                stale_lock_seconds=1,
                lease_ttl_seconds=1,
                heartbeat_interval_seconds=0.1,
            ),
        )

        def execute(node, current_graph):
            nonlocal started_at, finished_at
            started_at = time.time()
            if worker == "scheduler-a":
                lease_held.set()
            time.sleep(operation_delay_seconds)
            finished_at = time.time()
            return LeafExecutionResult(
                status="completed",
                summary=f"{worker} completed",
            )

        scheduler = TaskScheduler(
            decomposer=ScriptedTaskDecomposer({}),
            capability_resolver=InMemoryCapabilityResolver({_CAPABILITY}),
            leaf_executor=FunctionLeafExecutor(execute),
            integration_verifier=FunctionIntegrationVerifier(),
            policy=TaskSchedulerPolicy.create(
                parallel_safe_capabilities={_CAPABILITY},
                mutation_capabilities={_CAPABILITY},
                resource_claims={
                    "root": [ResourceClaim.workspace(shared, mode="write")]
                },
            ),
            resource_lease_manager=manager,
            retry_policy=retry_policy,
            retry_waiter=CancellableRetryWaiter(),
        )
        ready.put(worker)
        if not start.wait(timeout=5):
            raise TimeoutError("benchmark start signal timed out")
        if worker == "scheduler-b" and not lease_held.wait(timeout=5):
            raise TimeoutError("held lease signal timed out")
        completed = scheduler.run(graph)
        results.put(
            asdict(
                ContentionWorkerResult(
                    worker=worker,
                    status=str(completed.root.status),
                    started_at=started_at,
                    finished_at=finished_at,
                    planned_jitter_seconds=planned,
                    telemetry=aggregate_retry_telemetry(
                        completed
                    ).to_dict(),
                )
            )
        )
    except Exception as exc:
        results.put(
            asdict(
                ContentionWorkerResult(
                    worker=worker,
                    status="worker_failed",
                    started_at=started_at,
                    finished_at=finished_at,
                    planned_jitter_seconds=0.0,
                    telemetry={},
                    error=f"{type(exc).__name__}:{exc}",
                )
            )
        )


def _retry_policy(worker: str) -> TaskRetryPolicy:
    return TaskRetryPolicy.create(
        max_attempts_per_leaf=10,
        retryable_codes={_RETRY_CODE},
        idempotency_keys={"root": f"{worker}-write"},
        backoff_seconds=(0.05,) * 9,
        max_retry_elapsed_seconds=5.0,
        max_jitter_seconds=0.02,
        jitter_seed=worker,
    )


def _contention_result(worker: str) -> LeafExecutionResult:
    return LeafExecutionResult(
        status="blocked",
        summary="planned contention",
        retryable=True,
        retry_code=_RETRY_CODE,
        idempotency_key=f"{worker}-write",
    )


def _checks(
    workers: tuple[ContentionWorkerResult, ...],
    processes,
) -> tuple[BenchmarkAcceptanceCheck, ...]:
    completed = (
        len(workers) == 2
        and all(worker.status == "completed" for worker in workers)
    )
    intervals = [
        (worker.started_at, worker.finished_at)
        for worker in workers
        if worker.started_at is not None and worker.finished_at is not None
    ]
    overlap = (
        len(intervals) == 2
        and min(intervals[0][1], intervals[1][1])
        > max(intervals[0][0], intervals[1][0])
    )
    retries = sum(
        worker.telemetry.get("scheduled", 0) for worker in workers
    )
    jitters = {
        round(worker.planned_jitter_seconds, 12) for worker in workers
    }
    return (
        BenchmarkAcceptanceCheck(
            "both_schedulers_completed",
            completed,
            "both spawned scheduler processes reached completed",
        ),
        BenchmarkAcceptanceCheck(
            "write_leases_serialized",
            len(intervals) == 2 and not overlap,
            "write execution intervals did not overlap",
        ),
        BenchmarkAcceptanceCheck(
            "contention_retry_observed",
            retries >= 1,
            f"scheduled contention retries: {retries}",
        ),
        BenchmarkAcceptanceCheck(
            "jitter_identity_diverged",
            len(jitters) == 2 and all(value > 0 for value in jitters),
            "scheduler seeds produced distinct deterministic jitter",
        ),
        BenchmarkAcceptanceCheck(
            "workers_reaped",
            all(not process.is_alive() for process in processes),
            "all benchmark worker processes reached terminal state",
        ),
        BenchmarkAcceptanceCheck(
            "telemetry_is_secret_safe",
            all(
                "idempotency" not in json.dumps(worker.telemetry)
                for worker in workers
            ),
            "aggregate telemetry contains no idempotency keys",
        ),
    )


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("multiprocess contention benchmark timed out")
    return remaining
