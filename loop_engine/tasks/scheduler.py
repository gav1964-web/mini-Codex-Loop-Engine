"""Persistent iterative scheduler for atomic task graphs."""

from __future__ import annotations

from .events import record_task_event
from .decomposition_graph import add_child_tasks, validate_task_graph
from .models import (
    LeafExecutionResult,
    TaskGraph,
    TaskNode,
    TaskStatus,
)
from .parallel import (
    TaskSchedulerPolicy,
    execute_leaf_batch,
    select_leaf_batch,
    select_pending_candidate,
)
from .ports import (
    CapabilityAcquirer,
    CapabilityResolver,
    IntegrationVerifier,
    LeafExecutor,
    TaskDecomposer,
    TaskGraphStore,
)
from .resource_leases import (
    ResourceLeaseManager,
    batch_claims,
    record_resource_lease,
    run_leased_operation,
)
from .retry import (
    TaskRetryPolicy,
    evaluate_retry,
    retry_rejected_payload,
    retry_scheduled_payload,
)
from .scheduler_propagation import TaskSchedulerPropagation


class TaskScheduler(TaskSchedulerPropagation):
    def __init__(
        self,
        *,
        decomposer: TaskDecomposer,
        capability_resolver: CapabilityResolver,
        leaf_executor: LeafExecutor,
        integration_verifier: IntegrationVerifier,
        capability_acquirer: CapabilityAcquirer | None = None,
        store: TaskGraphStore | None = None,
        policy: TaskSchedulerPolicy | None = None,
        resource_lease_manager: ResourceLeaseManager | None = None,
        retry_policy: TaskRetryPolicy | None = None,
    ) -> None:
        self.decomposer = decomposer
        self.capability_resolver = capability_resolver
        self.capability_acquirer = capability_acquirer
        self.leaf_executor = leaf_executor
        self.integration_verifier = integration_verifier
        self.store = store
        self.policy = policy or TaskSchedulerPolicy()
        self.resource_lease_manager = resource_lease_manager
        self.retry_policy = retry_policy

    def run(self, graph: TaskGraph) -> TaskGraph:
        validate_task_graph(graph)
        record_task_event(
            graph,
            "task_graph_resumed" if graph.events else "task_graph_started",
            graph.root_id,
        )
        self._save(graph)

        while graph.root.status not in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
        }:
            changed = self._propagate(graph)
            if graph.root.status in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.BLOCKED,
            }:
                break

            candidates = self._candidate_nodes(graph)
            if not candidates:
                if changed:
                    continue
                graph.root.status = TaskStatus.BLOCKED
                graph.root.error = "task_graph_deadlock"
                graph.stop_reason = "task_graph_deadlock"
                record_task_event(graph, "task_graph_blocked", graph.root_id)
                self._save(graph)
                break

            pending = select_pending_candidate(candidates, self.policy)
            if pending is not None:
                self._assess_node(graph, pending)
                continue
            self._execute_ready_leaves(graph, candidates)

        graph.stop_reason = graph.root.error or (
            graph.root.result.summary if graph.root.result is not None else graph.stop_reason
        )
        record_task_event(
            graph,
            "task_graph_finished",
            graph.root_id,
            {"status": graph.root.status, "reason": graph.stop_reason},
        )
        self._save(graph)
        return graph

    def _assess_node(self, graph: TaskGraph, node: TaskNode) -> None:
        try:
            decision = self.decomposer.assess(node, graph)
        except Exception as exc:
            node.status = TaskStatus.FAILED
            node.error = f"decomposer_error:{type(exc).__name__}:{exc}"
            record_task_event(graph, "task_failed", node.id, {"error": node.error})
            self._save(graph)
            return
        record_task_event(
            graph,
            "atomicity_assessed",
            node.id,
            {"is_atomic": decision.is_atomic, "reason": decision.reason},
        )
        if decision.is_atomic:
            if decision.children:
                node.status = TaskStatus.FAILED
                node.error = "atomic_task_has_children"
                self._save(graph)
                return
            if decision.leaf is not None:
                node.goal = decision.leaf.goal
                node.success_criteria = list(decision.leaf.success_criteria)
                node.required_capabilities = list(
                    decision.leaf.required_capabilities
                )
                node.metadata.update(decision.leaf.metadata)
                record_task_event(
                    graph,
                    "atomic_leaf_contract_applied",
                    node.id,
                    {
                        "success_criteria": node.success_criteria,
                        "required_capabilities": node.required_capabilities,
                    },
                )
            node.status = TaskStatus.READY
            self._save(graph)
            return
        if decision.leaf is not None:
            node.status = TaskStatus.FAILED
            node.error = "non_atomic_task_has_leaf_contract"
            self._save(graph)
            return
        if not decision.children:
            node.status = TaskStatus.BLOCKED
            node.error = "non_atomic_task_has_no_children"
            self._save(graph)
            return
        if node.depth >= graph.budget.max_depth:
            node.status = TaskStatus.BLOCKED
            node.error = "task_depth_budget_exhausted"
            self._save(graph)
            return
        if len(graph.nodes) + len(decision.children) > graph.budget.max_nodes:
            node.status = TaskStatus.BLOCKED
            node.error = "task_node_budget_exhausted"
            self._save(graph)
            return
        try:
            add_child_tasks(graph, node, decision.children)
        except ValueError as exc:
            node.status = TaskStatus.FAILED
            node.error = f"decomposition_contract_error:{exc}"
            record_task_event(graph, "task_failed", node.id, {"error": node.error})
            self._save(graph)
            return
        node.status = TaskStatus.WAITING
        record_task_event(graph, "task_decomposed", node.id, {"children": node.children})
        self._save(graph)

    def _admit_leaf(self, graph: TaskGraph, node: TaskNode) -> bool:
        try:
            resolution = self.capability_resolver.resolve(node, graph)
        except Exception as exc:
            node.status = TaskStatus.BLOCKED
            node.error = f"capability_resolver_error:{type(exc).__name__}:{exc}"
            record_task_event(graph, "task_blocked", node.id, {"error": node.error})
            self._save(graph)
            return False
        missing = list(resolution.missing)
        if missing and self.capability_acquirer is not None:
            for capability in list(missing):
                record_task_event(
                    graph,
                    "capability_acquisition_requested",
                    node.id,
                    {"capability": capability},
                )
                try:
                    self.capability_acquirer.acquire(capability, node, graph)
                except Exception as exc:
                    record_task_event(
                        graph,
                        "capability_acquisition_failed",
                        node.id,
                        {
                            "capability": capability,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
            try:
                resolution = self.capability_resolver.resolve(node, graph)
            except Exception as exc:
                node.status = TaskStatus.BLOCKED
                node.error = f"capability_resolver_error:{type(exc).__name__}:{exc}"
                self._save(graph)
                return False
            missing = list(resolution.missing)
        if missing:
            node.status = TaskStatus.BLOCKED
            node.error = f"missing_capabilities:{','.join(sorted(missing))}"
            record_task_event(graph, "task_blocked", node.id, {"missing": missing})
            self._save(graph)
            return False
        return True

    def _start_leaf(self, graph: TaskGraph, node: TaskNode) -> bool:
        if graph.leaf_executions >= graph.budget.max_leaf_executions:
            node.status = TaskStatus.BLOCKED
            node.error = "leaf_execution_budget_exhausted"
            self._save(graph)
            return False

        node.status = TaskStatus.RUNNING
        node.attempts += 1
        graph.leaf_executions += 1
        record_task_event(graph, "leaf_execution_started", node.id)
        self._save(graph)
        return True

    def _execute_ready_leaves(
        self,
        graph: TaskGraph,
        candidates: list[TaskNode],
    ) -> None:
        selected = select_leaf_batch(candidates, self.policy)
        admitted = [
            node for node in selected if self._admit_leaf(graph, node)
        ]
        claims = batch_claims(admitted, self.policy)
        prepared: list[TaskNode] = []

        def execute():
            prepared.extend(
                node for node in admitted if self._start_leaf(graph, node)
            )
            return execute_leaf_batch(self.leaf_executor, prepared, graph)

        def record_lease(lease) -> None:
            record_resource_lease(graph, admitted, lease)
            self._save(graph)

        outcome = run_leased_operation(
            manager=self.resource_lease_manager,
            owner_id=f"{graph.id}:{','.join(node.id for node in admitted)}",
            claims=claims,
            on_acquired=record_lease,
            operation=execute,
        )
        if not outcome.executed:
            error = outcome.acquisition_error or outcome.heartbeat_error or (
                "resource_lease_unavailable:"
                + (",".join(outcome.conflicting_resources) or "unknown")
            )
            self._block_for_resource_lease(graph, admitted, error)
            return
        results = outcome.value
        lease_error = outcome.heartbeat_error or outcome.release_error
        if lease_error is not None:
            results = {
                node.id: LeafExecutionResult(
                    status="failed",
                    summary="resource lease lifecycle failed",
                    error=lease_error,
                )
                for node in prepared
            }
        for node in sorted(prepared, key=lambda item: item.id):
            self._apply_leaf_result(graph, node, results[node.id])

    def _block_for_resource_lease(
        self,
        graph: TaskGraph,
        nodes: list[TaskNode],
        error: str,
    ) -> None:
        for node in nodes:
            node.status = TaskStatus.BLOCKED
            node.error = error
            record_task_event(
                graph,
                "task_blocked",
                node.id,
                {"error": error},
            )
        self._save(graph)

    def _apply_leaf_result(
        self,
        graph: TaskGraph,
        node: TaskNode,
        result: LeafExecutionResult,
    ) -> None:
        node.result = result
        if result.status == "completed":
            node.status = TaskStatus.COMPLETED
            record_task_event(graph, "leaf_completed", node.id, {"summary": result.summary})
        elif result.status == "blocked":
            node.status = TaskStatus.BLOCKED
            node.error = result.error or result.summary
            record_task_event(graph, "leaf_blocked", node.id, {"error": node.error})
        else:
            node.error = result.error or result.summary
            decision = evaluate_retry(self.retry_policy, node, result)
            if decision.retry:
                node.status = TaskStatus.READY
                node.error = None
                record_task_event(
                    graph, "leaf_retry_scheduled", node.id,
                    retry_scheduled_payload(self.retry_policy, node, result),
                )
            else:
                node.status = TaskStatus.FAILED
                if result.retryable:
                    record_task_event(
                        graph, "leaf_retry_rejected", node.id,
                        retry_rejected_payload(decision, result),
                    )
                record_task_event(
                    graph,
                    "leaf_failed",
                    node.id,
                    {"error": node.error},
                )
        self._save(graph)

    @staticmethod
    def _candidate_nodes(graph: TaskGraph) -> list[TaskNode]:
        candidates = []
        for node in graph.nodes.values():
            if node.status not in {TaskStatus.PENDING, TaskStatus.READY}:
                continue
            if all(graph.nodes[item].status == TaskStatus.COMPLETED for item in node.dependencies):
                candidates.append(node)
        return sorted(candidates, key=lambda item: (item.depth, item.id))

    def _save(self, graph: TaskGraph) -> None:
        if self.store is not None:
            self.store.save(graph)
