from __future__ import annotations

import sys

import pytest

from loop_engine.tasks import (
    BoundedCommandIntegrationVerifier,
    BoundedIntegrationPolicy,
    ChildTaskSpec,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    IntegrationCommandSpec,
    LeafExecutionResult,
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskScheduler,
    TaskStatus,
)


def _command(source: str, *, cwd: str = ".") -> IntegrationCommandSpec:
    return IntegrationCommandSpec.create(
        [sys.executable, "-c", source],
        cwd=cwd,
    )


def _graph() -> TaskGraph:
    return TaskGraph.create("Integrate completed child work", graph_id="integration")


def _decomposer() -> ScriptedTaskDecomposer:
    return ScriptedTaskDecomposer(
        {
            "root": [
                ChildTaskSpec(key="one", goal="Write first marker"),
                ChildTaskSpec(key="two", goal="Write second marker"),
            ]
        }
    )


def _leaf_executor(tmp_path) -> FunctionLeafExecutor:
    def execute(node, graph):
        (tmp_path / f"{node.id}.done").write_text("done\n", encoding="utf-8")
        return LeafExecutionResult(
            status="completed",
            summary=f"{node.id} completed",
            evidence={"marker": f"{node.id}.done"},
        )

    return FunctionLeafExecutor(execute)


def _run(tmp_path, policy, *, graph=None):
    return TaskScheduler(
        decomposer=_decomposer(),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=_leaf_executor(tmp_path),
        integration_verifier=BoundedCommandIntegrationVerifier(policy),
    ).run(graph or _graph())


def test_parent_completes_only_after_bounded_integration_command(tmp_path) -> None:
    script = (
        "from pathlib import Path; "
        "required = ['root.one.done', 'root.two.done']; "
        "raise SystemExit(0 if all(Path(item).is_file() for item in required) else 1)"
    )
    policy = BoundedIntegrationPolicy.create(
        workspace_root=tmp_path,
        commands={"root": _command(script)},
        timeout_seconds=10,
    )

    result = _run(tmp_path, policy)

    assert result.root.status == TaskStatus.COMPLETED
    assert result.root.result is not None
    process = result.root.result.evidence["integration_process"]
    assert process["exit_code"] == 0
    assert process["timed_out"] is False
    assert set(result.root.result.evidence["children"]) == {
        "root.one",
        "root.two",
    }


def test_nonzero_integration_command_fails_completed_parent(tmp_path) -> None:
    policy = BoundedIntegrationPolicy.create(
        workspace_root=tmp_path,
        default_command=_command("raise SystemExit(7)"),
    )

    result = _run(tmp_path, policy)

    assert result.nodes["root.one"].status == TaskStatus.COMPLETED
    assert result.nodes["root.two"].status == TaskStatus.COMPLETED
    assert result.root.status == TaskStatus.FAILED
    assert result.root.error == "integration_command_exit_code:7"


def test_integration_timeout_blocks_parent(tmp_path) -> None:
    policy = BoundedIntegrationPolicy.create(
        workspace_root=tmp_path,
        default_command=_command("import time; time.sleep(10)"),
        timeout_seconds=0.1,
    )

    result = _run(tmp_path, policy)

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == "integration_command_timeout"


def test_task_metadata_cannot_override_integration_command(tmp_path) -> None:
    graph = _graph()
    graph.root.metadata = {
        "command": [sys.executable, "-c", "raise SystemExit(9)"],
        "cwd": "..",
        "timeout_seconds": 0.001,
    }
    policy = BoundedIntegrationPolicy.create(
        workspace_root=tmp_path,
        default_command=_command("print('configured command')"),
        timeout_seconds=10,
    )

    result = _run(tmp_path, policy, graph=graph)

    assert result.root.status == TaskStatus.COMPLETED
    assert (
        result.root.result.evidence["integration_process"]["stdout"].strip()
        == "configured command"
    )


def test_parent_without_matching_integration_command_is_blocked(tmp_path) -> None:
    policy = BoundedIntegrationPolicy.create(
        workspace_root=tmp_path,
        commands={"another.parent": _command("raise SystemExit(0)")},
    )

    result = _run(tmp_path, policy)

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == "integration_command_missing:root"


def test_integration_policy_rejects_cwd_outside_workspace(tmp_path) -> None:
    with pytest.raises(ValueError, match="cwd escapes workspace"):
        BoundedIntegrationPolicy.create(
            workspace_root=tmp_path,
            default_command=_command("raise SystemExit(0)", cwd=".."),
        )
