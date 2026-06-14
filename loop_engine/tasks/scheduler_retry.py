"""Retry-related state transitions owned by TaskScheduler."""

from __future__ import annotations

from .events import record_task_event
from .models import LeafExecutionResult, TaskGraph, TaskNode, TaskStatus
from .retry import (
    RetryDecision,
    evaluate_retry,
    retry_rejected_payload,
    retry_scheduled_payload,
)

LEASE_CONTENTION_RETRY_CODE = "resource_lease_contention"


class TaskSchedulerRetry:
    def _block_for_resource_lease(
        self,
        graph: TaskGraph,
        nodes: list[TaskNode],
        error: str,
        *,
        contention: bool,
    ) -> None:
        for node in nodes:
            key = (
                self.retry_policy.idempotency_key_for(node.id)
                if contention and self.retry_policy is not None
                else None
            )
            if key is None:
                node.status = TaskStatus.BLOCKED
                node.error = error
                record_task_event(
                    graph, "task_blocked", node.id, {"error": error}
                )
                continue
            self._apply_leaf_result(
                graph,
                node,
                LeafExecutionResult(
                    status="blocked",
                    summary=error,
                    error=error,
                    retryable=True,
                    retry_code=LEASE_CONTENTION_RETRY_CODE,
                    idempotency_key=key,
                ),
                save=False,
            )
        self._save(graph)

    def _apply_leaf_result(
        self,
        graph: TaskGraph,
        node: TaskNode,
        result: LeafExecutionResult,
        *,
        save: bool = True,
    ) -> None:
        node.result = result
        if result.status == "completed":
            node.status = TaskStatus.COMPLETED
            record_task_event(
                graph, "leaf_completed", node.id, {"summary": result.summary}
            )
        elif result.retryable:
            self._apply_retry_result(graph, node, result)
        elif result.status == "blocked":
            self._finish_leaf_blocked(graph, node, result)
        else:
            self._finish_leaf_failed(graph, node, result)
        if save:
            self._save(graph)

    def _apply_retry_result(
        self,
        graph: TaskGraph,
        node: TaskNode,
        result: LeafExecutionResult,
    ) -> None:
        decision = evaluate_retry(self.retry_policy, node, result)
        if decision.retry and not self._wait_for_retry(
            graph, node, result, decision
        ):
            decision = RetryDecision(False, "retry_wait_cancelled")
        if decision.retry:
            node.retries += 1
            node.status = TaskStatus.READY
            node.error = None
            record_task_event(
                graph,
                "leaf_retry_scheduled",
                node.id,
                retry_scheduled_payload(
                    self.retry_policy, node, result, decision
                ),
            )
            return
        record_task_event(
            graph,
            "leaf_retry_rejected",
            node.id,
            retry_rejected_payload(decision, result),
        )
        if result.status == "blocked":
            self._finish_leaf_blocked(graph, node, result)
        else:
            self._finish_leaf_failed(graph, node, result)

    def _wait_for_retry(
        self,
        graph: TaskGraph,
        node: TaskNode,
        result: LeafExecutionResult,
        decision: RetryDecision,
    ) -> bool:
        delay = decision.delay_seconds
        if delay <= 0:
            return True
        if self.retry_waiter is None:
            return False
        payload = {
            "delay_seconds": delay,
            "retry_code": result.retry_code,
        }
        record_task_event(
            graph, "leaf_retry_wait_started", node.id, payload
        )
        self._save(graph)
        try:
            completed = self.retry_waiter.wait(
                delay, node=node, graph=graph
            )
        except Exception as exc:
            completed = False
            payload = {
                **payload,
                "error": f"{type(exc).__name__}:{exc}",
            }
        record_task_event(
            graph,
            "leaf_retry_wait_completed",
            node.id,
            {**payload, "completed": completed},
        )
        return completed

    @staticmethod
    def _finish_leaf_blocked(
        graph: TaskGraph,
        node: TaskNode,
        result: LeafExecutionResult,
    ) -> None:
        node.status = TaskStatus.BLOCKED
        node.error = result.error or result.summary
        record_task_event(
            graph, "leaf_blocked", node.id, {"error": node.error}
        )

    @staticmethod
    def _finish_leaf_failed(
        graph: TaskGraph,
        node: TaskNode,
        result: LeafExecutionResult,
    ) -> None:
        node.status = TaskStatus.FAILED
        node.error = result.error or result.summary
        record_task_event(
            graph, "leaf_failed", node.id, {"error": node.error}
        )
