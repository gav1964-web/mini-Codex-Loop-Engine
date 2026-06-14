"""External bounded retry authority and cancellable backoff contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from threading import Event
import time
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

from .models import LeafExecutionResult, TaskGraph, TaskNode


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retry: bool
    reason: str
    delay_seconds: float = 0.0
    base_delay_seconds: float = 0.0
    jitter_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    remaining_seconds: float | None = None


class RetryClock(Protocol):
    def now(self) -> float:
        """Return a persistent epoch timestamp in seconds."""


class SystemRetryClock:
    def now(self) -> float:
        return time.time()


class RetryWaiter(Protocol):
    def wait(
        self,
        delay_seconds: float,
        *,
        node: TaskNode,
        graph: TaskGraph,
    ) -> bool:
        """Return false when the wait is cancelled."""


class CancellableRetryWaiter:
    """A bounded real-time waiter controlled by an external stop event."""

    def __init__(self, stop_event: Event | None = None) -> None:
        self.stop_event = stop_event or Event()

    def wait(
        self,
        delay_seconds: float,
        *,
        node: TaskNode,
        graph: TaskGraph,
    ) -> bool:
        del node, graph
        return not self.stop_event.wait(delay_seconds)

    def cancel(self) -> None:
        self.stop_event.set()


@dataclass(frozen=True, slots=True)
class TaskRetryPolicy:
    max_attempts_per_leaf: int
    retryable_codes: frozenset[str]
    idempotency_keys: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    backoff_seconds: tuple[float, ...] = ()
    max_retry_elapsed_seconds: float | None = None
    max_jitter_seconds: float = 0.0
    jitter_seed: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_attempts_per_leaf, int)
            or isinstance(self.max_attempts_per_leaf, bool)
            or self.max_attempts_per_leaf < 2
            or self.max_attempts_per_leaf > 10
        ):
            raise ValueError("retry max attempts must be between 2 and 10")
        codes = frozenset(code.strip() for code in self.retryable_codes)
        if not codes or "" in codes:
            raise ValueError("retryable codes must be non-empty")
        raw_keys = tuple(self.idempotency_keys.items())
        keys = {node_id.strip(): key.strip() for node_id, key in raw_keys}
        if not keys or "" in keys or "" in keys.values():
            raise ValueError("retry idempotency keys must be non-empty")
        if len(keys) != len(raw_keys):
            raise ValueError("retry idempotency node ids must be unique")
        delays = tuple(self.backoff_seconds)
        if len(delays) > self.max_attempts_per_leaf - 1:
            raise ValueError("retry backoff schedule exceeds retry budget")
        if any(
            not isinstance(delay, (int, float))
            or isinstance(delay, bool)
            or not math.isfinite(delay)
            or delay < 0
            or delay > 3600
            for delay in delays
        ):
            raise ValueError(
                "retry backoff delays must be between 0 and 3600 seconds"
            )
        window = self.max_retry_elapsed_seconds
        if (
            window is not None
            and (
                not isinstance(window, (int, float))
                or isinstance(window, bool)
                or not math.isfinite(window)
                or window <= 0
                or window > 86400
            )
        ):
            raise ValueError(
                "retry elapsed deadline must be between 0 and 86400 seconds"
            )
        jitter = self.max_jitter_seconds
        if (
            not isinstance(jitter, (int, float))
            or isinstance(jitter, bool)
            or not math.isfinite(jitter)
            or jitter < 0
            or jitter > 3600
        ):
            raise ValueError(
                "retry jitter must be between 0 and 3600 seconds"
            )
        seed = (self.jitter_seed or "").strip()
        if jitter > 0 and not seed:
            raise ValueError("retry jitter requires a non-empty seed")
        if jitter == 0 and self.jitter_seed is not None:
            raise ValueError("retry jitter seed requires positive jitter")
        object.__setattr__(self, "retryable_codes", codes)
        object.__setattr__(self, "idempotency_keys", MappingProxyType(keys))
        object.__setattr__(
            self,
            "backoff_seconds",
            tuple(float(delay) for delay in delays),
        )
        object.__setattr__(
            self,
            "max_retry_elapsed_seconds",
            None if window is None else float(window),
        )
        object.__setattr__(self, "max_jitter_seconds", float(jitter))
        object.__setattr__(self, "jitter_seed", seed or None)

    @classmethod
    def create(
        cls,
        *,
        max_attempts_per_leaf: int = 2,
        retryable_codes: set[str] | frozenset[str],
        idempotency_keys: Mapping[str, str],
        backoff_seconds: Sequence[float] = (),
        max_retry_elapsed_seconds: float | None = None,
        max_jitter_seconds: float = 0.0,
        jitter_seed: str | None = None,
    ) -> TaskRetryPolicy:
        return cls(
            max_attempts_per_leaf=max_attempts_per_leaf,
            retryable_codes=frozenset(retryable_codes),
            idempotency_keys=idempotency_keys,
            backoff_seconds=tuple(backoff_seconds),
            max_retry_elapsed_seconds=max_retry_elapsed_seconds,
            max_jitter_seconds=max_jitter_seconds,
            jitter_seed=jitter_seed,
        )

    def idempotency_key_for(self, node_id: str) -> str | None:
        return self.idempotency_keys.get(node_id)

    def decide(
        self,
        node: TaskNode,
        result: LeafExecutionResult,
        *,
        graph_id: str,
        now: float,
    ) -> RetryDecision:
        if (
            not isinstance(now, (int, float))
            or isinstance(now, bool)
            or not math.isfinite(now)
        ):
            return RetryDecision(False, "retry_clock_invalid")
        if not result.retryable:
            return RetryDecision(False, "retry_not_requested")
        if result.retry_code not in self.retryable_codes:
            return RetryDecision(False, "retry_code_not_allowed")
        expected_key = self.idempotency_keys.get(node.id)
        if expected_key is None:
            return RetryDecision(False, "retry_node_not_authorized")
        if result.idempotency_key != expected_key:
            return RetryDecision(False, "retry_idempotency_key_mismatch")
        if node.retries >= self.max_attempts_per_leaf - 1:
            return RetryDecision(False, "retry_attempt_budget_exhausted")
        base_delay = (
            self.backoff_seconds[node.retries]
            if node.retries < len(self.backoff_seconds)
            else 0.0
        )
        jitter = self._jitter_for(graph_id, node)
        delay = base_delay + jitter
        elapsed = 0.0
        remaining = self.max_retry_elapsed_seconds
        if node.retry_started_at is not None:
            if (
                not isinstance(node.retry_started_at, (int, float))
                or isinstance(node.retry_started_at, bool)
                or not math.isfinite(node.retry_started_at)
            ):
                return RetryDecision(False, "retry_clock_invalid")
            elapsed = now - node.retry_started_at
            if not math.isfinite(elapsed):
                return RetryDecision(False, "retry_clock_invalid")
            if elapsed < 0:
                return RetryDecision(False, "retry_clock_regressed")
            if remaining is not None:
                remaining -= elapsed
        if remaining is not None:
            if remaining <= 0:
                return RetryDecision(
                    False,
                    "retry_elapsed_budget_exhausted",
                    elapsed_seconds=elapsed,
                    remaining_seconds=max(0.0, remaining),
                )
            if delay > remaining:
                return RetryDecision(
                    False,
                    "retry_delay_exceeds_deadline",
                    delay_seconds=delay,
                    base_delay_seconds=base_delay,
                    jitter_seconds=jitter,
                    elapsed_seconds=elapsed,
                    remaining_seconds=remaining,
                )
        return RetryDecision(
            True,
            "retry_authorized",
            delay,
            base_delay,
            jitter,
            elapsed,
            remaining,
        )

    def _jitter_for(self, graph_id: str, node: TaskNode) -> float:
        if self.max_jitter_seconds <= 0:
            return 0.0
        value = (
            f"{self.jitter_seed}\0{graph_id}\0{node.id}\0{node.retries}"
        ).encode("utf-8")
        sample = int.from_bytes(
            hashlib.sha256(value).digest()[:8], "big"
        ) / ((1 << 64) - 1)
        return sample * self.max_jitter_seconds


def evaluate_retry(
    policy: TaskRetryPolicy | None,
    node: TaskNode,
    result: LeafExecutionResult,
    *,
    graph_id: str,
    now: float,
) -> RetryDecision:
    if policy is None:
        return RetryDecision(False, "retry_policy_missing")
    return policy.decide(node, result, graph_id=graph_id, now=now)


def retry_scheduled_payload(
    policy: TaskRetryPolicy,
    node: TaskNode,
    result: LeafExecutionResult,
    decision: RetryDecision,
) -> dict:
    return {
        "attempt": node.attempts,
        "retry": node.retries,
        "max_attempts": policy.max_attempts_per_leaf,
        "retry_code": result.retry_code,
        "idempotency_key": result.idempotency_key,
        "delay_seconds": decision.delay_seconds,
        "base_delay_seconds": decision.base_delay_seconds,
        "jitter_seconds": decision.jitter_seconds,
        "elapsed_seconds": decision.elapsed_seconds,
        "remaining_seconds": decision.remaining_seconds,
    }


def retry_rejected_payload(
    decision: RetryDecision,
    result: LeafExecutionResult,
) -> dict:
    return {
        "reason": decision.reason,
        "retry_code": result.retry_code,
        "elapsed_seconds": decision.elapsed_seconds,
        "remaining_seconds": decision.remaining_seconds,
    }
