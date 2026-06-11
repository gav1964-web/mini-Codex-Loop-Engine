from __future__ import annotations

import json

from loop_engine.demo import build_counter_demo
from loop_engine.cli import main
from loop_engine.tasks import (
    ChildTaskSpec,
    FunctionIntegrationVerifier,
    InMemoryCapabilityResolver,
    LoopEngineLeafExecutor,
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskScheduler,
    TaskStatus,
)


def test_atomic_leaf_executes_through_existing_loop_engine() -> None:
    graph = TaskGraph.create("Complete two atomic tasks", graph_id="loop-leaves")
    decomposer = ScriptedTaskDecomposer(
        {
            "root": [
                ChildTaskSpec(
                    key="one",
                    goal="Reach counter target one",
                    required_capabilities=["counter"],
                    metadata={"target": 1},
                ),
                ChildTaskSpec(
                    key="two",
                    goal="Reach counter target two",
                    required_capabilities=["counter"],
                    depends_on=["one"],
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

    result = TaskScheduler(
        decomposer=decomposer,
        capability_resolver=InMemoryCapabilityResolver({"counter"}),
        leaf_executor=LoopEngineLeafExecutor(factory),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.COMPLETED
    assert result.leaf_executions == 2
    assert result.nodes["root.one"].result.evidence["iterations"] == 1
    assert result.nodes["root.two"].result.evidence["iterations"] == 2
    assert result.nodes["root.two"].result.evidence["loop_status"] == "completed"


def test_task_demo_cli_persists_completed_graph(tmp_path, capsys) -> None:
    exit_code = main(["task-demo", "--graphs", str(tmp_path)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["nodes"]["root"]["status"] == "completed"
    assert output["leaf_executions"] == 2
    assert (tmp_path / "task-demo.json").exists()
