"""Deterministic task graph demo using LoopEngine-backed leaves."""

from __future__ import annotations

from pathlib import Path

from ..demo import build_counter_demo
from .adapters import (
    FunctionIntegrationVerifier,
    InMemoryCapabilityResolver,
    LoopEngineLeafExecutor,
    ScriptedTaskDecomposer,
)
from .models import ChildTaskSpec, TaskBudget, TaskGraph
from .persistence import JsonTaskGraphStore
from .scheduler import TaskScheduler


def build_task_demo(
    graph_store_root: str | Path | None = None,
) -> tuple[TaskScheduler, TaskGraph]:
    graph = TaskGraph.create(
        "Complete a dependency-ordered atomic task graph",
        graph_id="task-demo",
        budget=TaskBudget(max_nodes=8, max_depth=2, max_leaf_executions=4),
    )
    decomposer = ScriptedTaskDecomposer(
        {
            "root": [
                ChildTaskSpec(
                    key="inspect",
                    goal="Complete atomic inspection leaf",
                    required_capabilities=["counter"],
                    metadata={"target": 1},
                ),
                ChildTaskSpec(
                    key="execute",
                    goal="Complete atomic execution leaf",
                    required_capabilities=["counter"],
                    depends_on=["inspect"],
                    metadata={"target": 2},
                ),
            ]
        }
    )

    def factory(node, task_graph):
        engine, definition = build_counter_demo()
        definition.goal = node.goal
        definition.metadata["target"] = node.metadata["target"]
        return engine, definition

    store = JsonTaskGraphStore(graph_store_root) if graph_store_root else None
    scheduler = TaskScheduler(
        decomposer=decomposer,
        capability_resolver=InMemoryCapabilityResolver({"counter"}),
        leaf_executor=LoopEngineLeafExecutor(factory),
        integration_verifier=FunctionIntegrationVerifier(),
        store=store,
    )
    return scheduler, graph
