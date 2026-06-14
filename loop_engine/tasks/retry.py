"""External bounded retry authority for failed atomic leaves."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from .models import LeafExecutionResult, TaskNode


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retry: bool
    reason: str


@dataclass(frozen=True, slots=True)
class TaskRetryPolicy:
    max_attempts_per_leaf: int
    retryable_codes: frozenset[str]
    idempotency_keys: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )

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
        object.__setattr__(self, "retryable_codes", codes)
        object.__setattr__(self, "idempotency_keys", MappingProxyType(keys))

    @classmethod
    def create(
        cls,
        *,
        max_attempts_per_leaf: int = 2,
        retryable_codes: set[str] | frozenset[str],
        idempotency_keys: Mapping[str, str],
    ) -> TaskRetryPolicy:
        return cls(
            max_attempts_per_leaf=max_attempts_per_leaf,
            retryable_codes=frozenset(retryable_codes),
            idempotency_keys=idempotency_keys,
        )

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
        if node.attempts >= self.max_attempts_per_leaf:
            return RetryDecision(False, "retry_attempt_budget_exhausted")
        return RetryDecision(True, "retry_authorized")


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
) -> dict:
    return {
        "attempt": node.attempts,
        "max_attempts": policy.max_attempts_per_leaf,
        "retry_code": result.retry_code,
        "idempotency_key": result.idempotency_key,
    }


def retry_rejected_payload(
    decision: RetryDecision,
    result: LeafExecutionResult,
) -> dict:
    return {
        "reason": decision.reason,
        "retry_code": result.retry_code,
    }
