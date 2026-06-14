"""External bounded retry authority and cancellable backoff contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

from .models import LeafExecutionResult, TaskGraph, TaskNode


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retry: bool
    reason: str
    delay_seconds: float = 0.0


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
            or delay < 0
            or delay > 3600
            for delay in delays
        ):
            raise ValueError(
                "retry backoff delays must be between 0 and 3600 seconds"
            )
        object.__setattr__(self, "retryable_codes", codes)
        object.__setattr__(self, "idempotency_keys", MappingProxyType(keys))
        object.__setattr__(
            self,
            "backoff_seconds",
            tuple(float(delay) for delay in delays),
        )

    @classmethod
    def create(
        cls,
        *,
        max_attempts_per_leaf: int = 2,
        retryable_codes: set[str] | frozenset[str],
        idempotency_keys: Mapping[str, str],
        backoff_seconds: Sequence[float] = (),
    ) -> TaskRetryPolicy:
        return cls(
            max_attempts_per_leaf=max_attempts_per_leaf,
            retryable_codes=frozenset(retryable_codes),
            idempotency_keys=idempotency_keys,
            backoff_seconds=tuple(backoff_seconds),
        )

    def idempotency_key_for(self, node_id: str) -> str | None:
        return self.idempotency_keys.get(node_id)

    def decide(
        self,
        node: TaskNode,
        result: LeafExecutionResult,
    ) -> RetryDecision:
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
        delay = (
            self.backoff_seconds[node.retries]
            if node.retries < len(self.backoff_seconds)
            else 0.0
        )
        return RetryDecision(True, "retry_authorized", delay)


def evaluate_retry(
    policy: TaskRetryPolicy | None,
    node: TaskNode,
    result: LeafExecutionResult,
) -> RetryDecision:
    if policy is None:
        return RetryDecision(False, "retry_policy_missing")
    return policy.decide(node, result)


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
    }


def retry_rejected_payload(
    decision: RetryDecision,
    result: LeafExecutionResult,
) -> dict:
    return {
        "reason": decision.reason,
        "retry_code": result.retry_code,
    }
