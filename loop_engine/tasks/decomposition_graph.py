"""Validated graph mutation for one decomposition decision."""

from __future__ import annotations

from .models import ChildTaskSpec, TaskGraph, TaskNode


def validate_task_graph(graph: TaskGraph) -> None:
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
            raise ValueError(
                f"task node {node.id} has missing references: {missing}"
            )


def add_child_tasks(
    graph: TaskGraph,
    parent: TaskNode,
    specs: list[ChildTaskSpec],
) -> None:
    keys = [spec.key for spec in specs]
    if len(keys) != len(set(keys)) or any(not key.strip() for key in keys):
        raise ValueError("child task keys must be unique and non-empty")
    key_set = set(keys)
    for spec in specs:
        unknown_dependencies = set(spec.depends_on) - key_set
        if unknown_dependencies:
            raise ValueError(
                f"unknown child dependencies for {spec.key}: "
                f"{sorted(unknown_dependencies)}"
            )
    _validate_dependency_graph(specs)
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


def _validate_dependency_graph(specs: list[ChildTaskSpec]) -> None:
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
