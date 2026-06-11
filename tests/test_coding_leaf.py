from __future__ import annotations

import sys

from loop_engine.tasks import (
    ChildTaskSpec,
    CodingLeafExecutor,
    CodingLeafPolicy,
    FunctionIntegrationVerifier,
    InMemoryCapabilityResolver,
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskScheduler,
    TaskStatus,
)


class SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def complete_json(self, messages):
        self.messages.append(messages)
        return self.responses.pop(0)


def _verification_command(expected: int) -> list[str]:
    return [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; ns = {}; "
            "exec(Path('target.py').read_text(), ns); "
            f"raise SystemExit(0 if ns['value'] == {expected} else 1)"
        ),
    ]


def _policy(tmp_path, *, expected: int = 2) -> CodingLeafPolicy:
    return CodingLeafPolicy.create(
        workspace_root=tmp_path,
        verification_command=_verification_command(expected),
        subprocess_timeout_seconds=10,
    )


def test_verify_leaf_runs_only_external_immutable_command(tmp_path) -> None:
    (tmp_path / "target.py").write_text("value = 2\n", encoding="utf-8")
    graph = TaskGraph.create(
        "Verify target",
        success_criteria=["Configured verification exits with code 0"],
        required_capabilities=["process.verify"],
    )
    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver({"process.verify"}),
        leaf_executor=CodingLeafExecutor(_policy(tmp_path)),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.COMPLETED
    assert result.root.result.evidence["coding_leaf_profile"] == "coding_check"
    assert result.root.result.evidence["verification"]["exit_code"] == 0


def test_repair_leaf_runs_validated_llm_loop_and_verification(tmp_path) -> None:
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    client = SequenceClient(
        [
            {
                "rationale": "inspect, repair, verify",
                "actions": [
                    {
                        "tool": "read_text",
                        "arguments": {"path": "target.py"},
                        "reason": "inspect current value",
                    },
                    {
                        "tool": "apply_patch",
                        "arguments": {
                            "path": "target.py",
                            "old_text": "value = 1",
                            "new_text": "value = 2",
                        },
                        "reason": "apply bounded repair",
                    },
                    {
                        "tool": "run_verification",
                        "arguments": {},
                        "reason": "verify repaired workspace",
                    },
                ],
                "expected_evidence": ["verification exit code 0"],
            }
        ]
    )
    graph = TaskGraph.create(
        "Change target.py value from 1 to 2 and verify it",
        success_criteria=["target.py contains value = 2", "verification passes"],
        required_capabilities=[
            "filesystem.read",
            "filesystem.patch",
            "process.verify",
        ],
    )
    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(
            {"filesystem.read", "filesystem.patch", "process.verify"}
        ),
        leaf_executor=CodingLeafExecutor(_policy(tmp_path), llm_client=client),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.COMPLETED
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert result.root.result.evidence["coding_leaf_profile"] == "llm_repair"
    assert len(client.messages) == 1


def test_dependency_ordered_repair_then_verify_task_tree(tmp_path) -> None:
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    client = SequenceClient(
        [
            {
                "actions": [
                    {
                        "tool": "apply_patch",
                        "arguments": {
                            "path": "target.py",
                            "old_text": "value = 1",
                            "new_text": "value = 2",
                        },
                    },
                    {"tool": "run_verification", "arguments": {}},
                ]
            }
        ]
    )
    graph = TaskGraph.create("Repair and independently verify target")
    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer(
            {
                "root": [
                    ChildTaskSpec(
                        key="repair",
                        goal="Apply the bounded target repair",
                        success_criteria=["target.py contains value = 2"],
                        required_capabilities=[
                            "filesystem.patch",
                            "process.verify",
                        ],
                    ),
                    ChildTaskSpec(
                        key="verify",
                        goal="Independently verify the repaired target",
                        success_criteria=["Configured verification exits with code 0"],
                        required_capabilities=["process.verify"],
                        depends_on=["repair"],
                    ),
                ]
            }
        ),
        capability_resolver=InMemoryCapabilityResolver(
            {"filesystem.patch", "process.verify"}
        ),
        leaf_executor=CodingLeafExecutor(_policy(tmp_path), llm_client=client),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.COMPLETED
    assert result.nodes["root.repair"].status == TaskStatus.COMPLETED
    assert result.nodes["root.verify"].status == TaskStatus.COMPLETED
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_read_only_leaf_uses_evidence_profile(tmp_path) -> None:
    (tmp_path / "target.py").write_text(
        "def calculate_total(items):\n    return sum(items)\n",
        encoding="utf-8",
    )
    criterion = "target.py defines calculate_total"
    client = SequenceClient(
        [
            {
                "actions": [
                    {
                        "tool": "read_text",
                        "arguments": {"path": "target.py"},
                    }
                ]
            },
            {
                "criteria": [
                    {
                        "criterion": criterion,
                        "satisfied": True,
                        "evidence_refs": ["evidence:0"],
                        "reason": "direct function definition",
                    }
                ],
                "missing_evidence": [],
                "summary": "criterion supported",
            },
        ]
    )
    graph = TaskGraph.create(
        "Inspect target",
        success_criteria=[criterion],
        required_capabilities=["filesystem.read"],
    )
    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver({"filesystem.read"}),
        leaf_executor=CodingLeafExecutor(
            CodingLeafPolicy.create(workspace_root=tmp_path),
            llm_client=client,
        ),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.COMPLETED
    assert result.root.attempts == 1
    assert result.root.result.evidence["coding_leaf_profile"] == "llm_evidence"


def test_verify_leaf_without_external_command_is_blocked(tmp_path) -> None:
    graph = TaskGraph.create(
        "Verify target",
        success_criteria=["Verification passes"],
        required_capabilities=["process.verify"],
    )
    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver({"process.verify"}),
        leaf_executor=CodingLeafExecutor(
            CodingLeafPolicy.create(workspace_root=tmp_path)
        ),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == "coding_leaf_verification_command_missing"


def test_patch_leaf_requires_verification_capability(tmp_path) -> None:
    graph = TaskGraph.create(
        "Patch target",
        success_criteria=["Target is repaired"],
        required_capabilities=["filesystem.patch"],
    )
    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver({"filesystem.patch"}),
        leaf_executor=CodingLeafExecutor(_policy(tmp_path)),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == "coding_patch_requires_process_verify"


def test_llm_metadata_cannot_override_workspace_or_command(tmp_path) -> None:
    graph = TaskGraph.create(
        "Verify target",
        success_criteria=["Verification passes"],
        required_capabilities=["process.verify"],
    )
    graph.root.metadata = {
        "workspace_root": "C:/",
        "command": ["powershell", "-Command", "Write-Output unsafe"],
    }
    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver({"process.verify"}),
        leaf_executor=CodingLeafExecutor(_policy(tmp_path)),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == "reserved_coding_metadata:command,workspace_root"


def test_repair_leaf_without_llm_client_is_blocked(tmp_path) -> None:
    graph = TaskGraph.create(
        "Repair target",
        success_criteria=["Verification passes"],
        required_capabilities=["filesystem.patch", "process.verify"],
    )
    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(
            {"filesystem.patch", "process.verify"}
        ),
        leaf_executor=CodingLeafExecutor(_policy(tmp_path)),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == "coding_leaf_llm_client_missing"
