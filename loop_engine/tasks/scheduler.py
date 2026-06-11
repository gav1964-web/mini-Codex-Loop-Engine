"""Persistent iterative scheduler for atomic task graphs."""

from __future__ import annotations

from .events import record_task_event
from .models import (
    LeafExecutionResult,
    TaskGraph,
    TaskNode,
    TaskStatus,
)
from .ports import (
    CapabilityAcquirer,
    CapabilityResolver,
    IntegrationVerifier,
    LeafExecutor,
    TaskDecomposer,
    TaskGraphStore,
)


class TaskScheduler:
    def __init__(
        self,
        *,
        decomposer: TaskDecomposer,
        capability_resolver: CapabilityResolver,
        leaf_executor: LeafExecutor,
        integration_verifier: IntegrationVerifier,
        capability_acquirer: CapabilityAcquirer | None = None,
        store: TaskGraphStore | None = None,
    ) -> None:
        self.decomposer = decomposer
        self.capability_resolver = capability_resolver
        self.capability_acquirer = capability_acquirer
        self.leaf_executor = leaf_executor
        self.integration_verifier = integration_verifier
        self.store = store

    def run(self, graph: TaskGraph) -> TaskGraph:
        self._validate_graph(graph)
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

            node = self._next_node(graph)
            if node is None:
                if changed:
                    continue
                graph.root.status = TaskStatus.BLOCKED
                graph.root.error = "task_graph_deadlock"
                graph.stop_reason = "task_graph_deadlock"
                record_task_event(graph, "task_graph_blocked", graph.root_id)
                self._save(graph)
                break

            if node.status == TaskStatus.PENDING:
                self._assess_node(graph, node)
            elif node.status == TaskStatus.READY:
                self._execute_leaf(graph, node)

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
            self._add_children(graph, node, decision.children)
        except ValueError as exc:
            node.status = TaskStatus.FAILED
            node.error = f"decomposition_contract_error:{exc}"
            record_task_event(graph, "task_failed", node.id, {"error": node.error})
            self._save(graph)
            return
        node.status = TaskStatus.WAITING
        record_task_event(graph, "task_decomposed", node.id, {"children": node.children})
        self._save(graph)

    def _add_children(self, graph: TaskGraph, parent: TaskNode, specs) -> None:
        keys = [spec.key for spec in specs]
        if len(keys) != len(set(keys)) or any(not key.strip() for key in keys):
            raise ValueError("child task keys must be unique and non-empty")
        key_set = set(keys)
        for spec in specs:
            unknown_dependencies = set(spec.depends_on) - key_set
            if unknown_dependencies:
                raise ValueError(
                    f"unknown child dependencies for {spec.key}: {sorted(unknown_dependencies)}"
                )
        self._validate_child_dependency_graph(specs)
        key_to_id = {key: f"{parent.id}.{key}" for key in keys}
        for spec in specs:
            child_id = key_to_id[spec.key]
            graph.nodes[child_id] = TaskNode(
                id=child_id,
                parent_id=parent.id,
                goal=spec.goal,
                success_criteria=list(spec.success_criteria),
                required_capabilities=list(spec.required_capabilities),
                dependencies=[key_to_id[key] for key in spec.depends_on],
                depth=parent.depth + 1,
                metadata=dict(spec.metadata),
            )
            parent.children.append(child_id)

    @staticmethod
    def _validate_child_dependency_graph(specs) -> None:
        dependencies = {spec.key: list(spec.depends_on) for spec in specs}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("child task dependencies contain a cycle")
            if key in visited:
                return
            visiting.add(key)
            for dependency in dependencies[key]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in dependencies:
            visit(key)

    def _execute_leaf(self, graph: TaskGraph, node: TaskNode) -> None:
        try:
            resolution = self.capability_resolver.resolve(node, graph)
        except Exception as exc:
            node.status = TaskStatus.BLOCKED
            node.error = f"capability_resolver_error:{type(exc).__name__}:{exc}"
            record_task_event(graph, "task_blocked", node.id, {"error": node.error})
            self._save(graph)
            return
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
                return
            missing = list(resolution.missing)
        if missing:
            node.status = TaskStatus.BLOCKED
            node.error = f"missing_capabilities:{','.join(sorted(missing))}"
            record_task_event(graph, "task_blocked", node.id, {"missing": missing})
            self._save(graph)
            return
        if graph.leaf_executions >= graph.budget.max_leaf_executions:
            node.status = TaskStatus.BLOCKED
            node.error = "leaf_execution_budget_exhausted"
            self._save(graph)
            return

        node.status = TaskStatus.RUNNING
        node.attempts += 1
        graph.leaf_executions += 1
        record_task_event(graph, "leaf_execution_started", node.id)
        self._save(graph)
        try:
            result = self.leaf_executor.execute(node, graph)
        except Exception as exc:
            result = LeafExecutionResult(
                status="failed",
                summary="leaf executor raised an exception",
                error=f"{type(exc).__name__}: {exc}",
            )
        node.result = result
        if result.status == "completed":
            node.status = TaskStatus.COMPLETED
            record_task_event(graph, "leaf_completed", node.id, {"summary": result.summary})
        elif result.status == "blocked":
            node.status = TaskStatus.BLOCKED
            node.error = result.error or result.summary
            record_task_event(graph, "leaf_blocked", node.id, {"error": node.error})
        else:
            node.status = TaskStatus.FAILED
            node.error = result.error or result.summary
            record_task_event(graph, "leaf_failed", node.id, {"error": node.error})
        self._save(graph)

    def _propagate(self, graph: TaskGraph) -> bool:
        changed = False
        for node in sorted(graph.nodes.values(), key=lambda item: item.depth, reverse=True):
            if node.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}:
                continue
            dependency_states = [graph.nodes[item].status for item in node.dependencies]
            if any(status in {TaskStatus.FAILED, TaskStatus.BLOCKED} for status in dependency_states):
                node.status = TaskStatus.BLOCKED
                node.error = "dependency_failed_or_blocked"
                record_task_event(graph, "task_blocked", node.id, {"reason": node.error})
                self._save(graph)
                changed = True
                continue
            if node.children:
                child_states = [graph.nodes[item].status for item in node.children]
                if any(status in {TaskStatus.FAILED, TaskStatus.BLOCKED} for status in child_states):
                    node.status = TaskStatus.BLOCKED
                    node.error = "child_failed_or_blocked"
                    record_task_event(graph, "task_blocked", node.id, {"reason": node.error})
                    self._save(graph)
                    changed = True
                elif all(status == TaskStatus.COMPLETED for status in child_states):
                    try:
                        result = self.integration_verifier.verify(node, graph)
                    except Exception as exc:
                        result = LeafExecutionResult(
                            status="failed",
                            summary="integration verifier raised an exception",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    node.result = result
                    if result.status == "completed":
                        node.status = TaskStatus.COMPLETED
                        record_task_event(
                            graph,
                            "integration_completed",
                            node.id,
                            {"summary": result.summary},
                        )
                    else:
                        node.status = (
                            TaskStatus.BLOCKED
                            if result.status == "blocked"
                            else TaskStatus.FAILED
                        )
                        node.error = result.error or result.summary
                        record_task_event(
                            graph,
                            "integration_failed",
                            node.id,
                            {"error": node.error},
                        )
                    self._save(graph)
                    changed = True
        return changed

    @staticmethod
    def _next_node(graph: TaskGraph) -> TaskNode | None:
        candidates = []
        for node in graph.nodes.values():
            if node.status not in {TaskStatus.PENDING, TaskStatus.READY}:
                continue
            if all(graph.nodes[item].status == TaskStatus.COMPLETED for item in node.dependencies):
                candidates.append(node)
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: (item.depth, item.id))[0]

    @staticmethod
    def _validate_graph(graph: TaskGraph) -> None:
        if graph.root_id not in graph.nodes:
            raise ValueError("task graph root is missing")
        if graph.budget.max_nodes <= 0 or graph.budget.max_depth < 0:
            raise ValueError("invalid task graph budget")
        if graph.budget.max_leaf_executions <= 0:
            raise ValueError("max_leaf_executions must be positive")
        if len(graph.nodes) > graph.budget.max_nodes:
            raise ValueError("task graph already exceeds max_nodes")
        for node in graph.nodes.values():
            references = [*node.dependencies, *node.children]
            missing = [item for item in references if item not in graph.nodes]
            if missing:
                raise ValueError(f"task node {node.id} has missing references: {missing}")

    def _save(self, graph: TaskGraph) -> None:
        if self.store is not None:
            self.store.save(graph)
