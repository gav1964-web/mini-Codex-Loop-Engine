from __future__ import annotations

import json

import pytest

from loop_engine import Action, ActionResult, LoopDefinition, LoopState, LoopStatus
from loop_engine.adapters import EvidenceContractError, ValidatedEvidenceVerifier
from loop_engine.profiles import build_llm_evidence_loop


class SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def complete_json(self, messages):
        self.messages.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _assessment(criterion: str, *, ref: str = "evidence:0") -> dict:
    return {
        "criteria": [
            {
                "criterion": criterion,
                "satisfied": True,
                "evidence_refs": [ref],
                "reason": "the source contains the required fact",
            }
        ],
        "missing_evidence": [],
        "summary": "criterion is supported",
    }


def test_read_only_profile_collects_and_verifies_evidence(tmp_path) -> None:
    (tmp_path / "target.py").write_text(
        "def calculate_total(items):\n    return sum(items)\n",
        encoding="utf-8",
    )
    criterion = "target.py defines calculate_total"
    client = SequenceClient(
        [
            {
                "rationale": "inspect target source",
                "actions": [
                    {
                        "tool": "read_text",
                        "arguments": {"path": "target.py"},
                        "reason": "collect direct source evidence",
                    }
                ],
                "expected_evidence": ["function definition"],
            },
            _assessment(criterion),
        ]
    )
    engine, definition = build_llm_evidence_loop(
        workspace_root=tmp_path,
        goal="Determine whether target.py defines calculate_total",
        success_criteria=[criterion],
        llm_client=client,
        allowed_tools={"read_text"},
    )

    state = engine.run(definition)

    assert state.status == LoopStatus.COMPLETED
    assert [result.action.tool for result in state.action_results] == ["read_text"]
    assert state.latest_verification.evidence["catalogue_ids"] == ["evidence:0"]
    verifier_request = json.loads(client.messages[1][1]["content"])
    assert "calculate_total" in json.dumps(
        verifier_request["evidence_catalogue"],
        ensure_ascii=False,
    )


def test_read_only_profile_rejects_mutating_plan_before_execution(tmp_path) -> None:
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    invalid = {
        "actions": [
            {
                "tool": "apply_patch",
                "arguments": {
                    "path": "target.py",
                    "old_text": "value = 1",
                    "new_text": "value = 2",
                },
            }
        ]
    }
    client = SequenceClient([invalid, invalid])
    engine, definition = build_llm_evidence_loop(
        workspace_root=tmp_path,
        goal="Inspect target",
        success_criteria=["target.py has an integer value"],
        llm_client=client,
        allowed_tools={"read_text"},
    )

    state = engine.run(definition)

    assert state.status == LoopStatus.FAILED
    assert state.action_count == 0
    assert "tool is not allowed by this profile" in state.stop_reason
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_read_only_executor_physically_has_no_patch_tool(tmp_path) -> None:
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    client = SequenceClient([])
    engine, definition = build_llm_evidence_loop(
        workspace_root=tmp_path,
        goal="Inspect target",
        success_criteria=["target.py has an integer value"],
        llm_client=client,
        allowed_tools={"read_text"},
    )
    state = engine.create_state(definition, run_id="read-only-registry")

    result = engine.executor.execute(
        Action(
            tool="apply_patch",
            arguments={
                "path": "target.py",
                "old_text": "value = 1",
                "new_text": "value = 2",
            },
        ),
        state,
    )

    assert result.status == "error"
    assert result.error == "unknown tool: apply_patch"
    assert target.read_text(encoding="utf-8") == "value = 1\n"

    undeclared = engine.executor.execute(
        Action(tool="search_text", arguments={"query": "value"}),
        state,
    )
    assert undeclared.status == "error"
    assert undeclared.error == "unknown tool: search_text"


def test_read_only_profile_rejects_empty_tool_set(tmp_path) -> None:
    with pytest.raises(ValueError, match="allowed_tools must be non-empty"):
        build_llm_evidence_loop(
            workspace_root=tmp_path,
            goal="Inspect target",
            success_criteria=["evidence exists"],
            llm_client=SequenceClient([]),
            allowed_tools=set(),
        )


def test_evidence_verifier_rejects_unknown_reference_after_repair() -> None:
    criterion = "source proves the fact"
    invalid = _assessment(criterion, ref="evidence:999")
    client = SequenceClient([invalid, invalid])
    state = LoopState(
        run_id="evidence",
        definition=LoopDefinition(
            goal="Inspect source",
            success_criteria=[criterion],
        ),
    )
    state.action_results = [
        ActionResult(
            action=Action(tool="read_text", arguments={"path": "target.py"}),
            status="ok",
            output={"text": "fact = True"},
        )
    ]

    with pytest.raises(EvidenceContractError, match="unknown evidence refs"):
        ValidatedEvidenceVerifier(client).verify(
            state,
            state.action_results,
        )

    assert len(client.messages) == 2


def test_satisfied_criterion_without_reference_fails_after_repair() -> None:
    criterion = "source proves the fact"
    invalid = _assessment(criterion)
    invalid["criteria"][0]["evidence_refs"] = []
    client = SequenceClient([invalid, invalid])
    state = LoopState(
        run_id="evidence",
        definition=LoopDefinition(
            goal="Inspect source",
            success_criteria=[criterion],
        ),
    )
    state.action_results = [
        ActionResult(
            action=Action(tool="read_text", arguments={"path": "target.py"}),
            status="ok",
            output={"text": "fact = True"},
        )
    ]

    with pytest.raises(EvidenceContractError, match="requires evidence_refs"):
        ValidatedEvidenceVerifier(client).verify(state, state.action_results)

    assert len(client.messages) == 2


def test_evidence_transport_error_is_not_contract_repaired() -> None:
    client = SequenceClient([RuntimeError("gateway unavailable")])
    state = LoopState(
        run_id="evidence",
        definition=LoopDefinition(
            goal="Inspect source",
            success_criteria=["source proves the fact"],
        ),
    )
    state.action_results = [
        ActionResult(
            action=Action(tool="read_text", arguments={"path": "target.py"}),
            status="ok",
            output={"text": "fact = True"},
        )
    ]

    with pytest.raises(RuntimeError, match="gateway unavailable"):
        ValidatedEvidenceVerifier(client).verify(state, state.action_results)

    assert len(client.messages) == 1


def test_evidence_verifier_repairs_missing_criterion() -> None:
    criterion = "source proves the fact"
    client = SequenceClient(
        [
            {
                "criteria": [],
                "missing_evidence": [],
                "summary": "bad",
            },
            _assessment(criterion),
        ]
    )
    state = LoopState(
        run_id="evidence",
        definition=LoopDefinition(
            goal="Inspect source",
            success_criteria=[criterion],
        ),
    )
    state.action_results = [
        ActionResult(
            action=Action(tool="read_text", arguments={"path": "target.py"}),
            status="ok",
            output={"text": "fact = True"},
        )
    ]

    result = ValidatedEvidenceVerifier(client).verify(
        state,
        state.action_results,
    )

    assert result.status == "passed"
    repair_request = json.loads(client.messages[1][1]["content"])
    assert repair_request["allowed_evidence_refs"] == ["evidence:0"]
    assert repair_request["repair_attempts_remaining"] == 0


def test_unsatisfied_evidence_replans_then_completes(tmp_path) -> None:
    (tmp_path / "target.py").write_text(
        "class Service:\n    timeout = 30\n",
        encoding="utf-8",
    )
    criterion = "Service timeout equals 30"
    client = SequenceClient(
        [
            {
                "actions": [
                    {
                        "tool": "search_text",
                        "arguments": {"query": "Service"},
                    }
                ]
            },
            {
                "criteria": [
                    {
                        "criterion": criterion,
                        "satisfied": False,
                        "evidence_refs": ["evidence:0"],
                        "reason": "class found but timeout value is not shown",
                    }
                ],
                "missing_evidence": ["read target.py to inspect timeout"],
                "summary": "more evidence required",
            },
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
                        "evidence_refs": ["evidence:1"],
                        "reason": "target.py directly assigns timeout = 30",
                    }
                ],
                "missing_evidence": [],
                "summary": "criterion supported",
            },
        ]
    )
    engine, definition = build_llm_evidence_loop(
        workspace_root=tmp_path,
        goal="Inspect Service timeout",
        success_criteria=[criterion],
        llm_client=client,
        allowed_tools={"search_text", "read_text"},
    )

    state = engine.run(definition)

    assert state.status == LoopStatus.COMPLETED
    assert state.iteration == 2
    assert [result.action.tool for result in state.action_results] == [
        "search_text",
        "read_text",
    ]
