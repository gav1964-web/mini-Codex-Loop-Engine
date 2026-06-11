from __future__ import annotations

import json
import sys

from loop_engine import LoopStatus
from loop_engine.cli import main
from loop_engine.profiles import build_llm_repair_loop


class SequenceJSONClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.messages = []

    def complete_json(self, messages):
        self.messages.append(messages)
        return self.payloads.pop(0)


def test_llm_repair_inspects_then_repairs_and_verifies(tmp_path) -> None:
    target = tmp_path / "target.py"
    target.write_text("def value():\n    return 1\n", encoding="utf-8")
    client = SequenceJSONClient(
        [
            {
                "rationale": "inspect the target",
                "actions": [
                    {
                        "tool": "read_text",
                        "arguments": {"path": "target.py"},
                        "reason": "need exact source",
                    }
                ],
                "expected_evidence": ["target source"],
            },
            {
                "rationale": "apply exact repair and verify",
                "actions": [
                    {
                        "tool": "apply_patch",
                        "arguments": {
                            "path": "target.py",
                            "old_text": "return 1",
                            "new_text": "return 2",
                        },
                        "reason": "fix value",
                    },
                    {
                        "tool": "run_verification",
                        "arguments": {},
                        "reason": "prove repair",
                    },
                ],
                "expected_evidence": ["exit code 0"],
            },
        ]
    )
    engine, definition = build_llm_repair_loop(
        workspace_root=tmp_path,
        goal="Make target.value return 2",
        llm_client=client,
        verification_command=[
            sys.executable,
            "-c",
            "from target import value; raise SystemExit(0 if value() == 2 else 1)",
        ],
    )

    state = engine.run(definition)

    assert state.status == LoopStatus.COMPLETED
    assert state.iteration == 2
    assert [result.action.tool for result in state.action_results] == [
        "read_text",
        "apply_patch",
        "run_verification",
    ]
    second_context = json.loads(client.messages[1][1]["content"])
    assert "return 1" in second_context["recent_results"][0]["output"]["text"]
    assert "return 2" in target.read_text(encoding="utf-8")


def test_invalid_llm_plan_fails_before_any_tool_execution(tmp_path) -> None:
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    client = SequenceJSONClient(
        [
            {"actions": [{"tool": "shell", "arguments": {"command": "del target.py"}}]},
            {"actions": [{"tool": "shell", "arguments": {"command": "del target.py"}}]},
        ]
    )
    engine, definition = build_llm_repair_loop(
        workspace_root=tmp_path,
        goal="Repair target",
        llm_client=client,
        verification_command=[sys.executable, "-c", "raise SystemExit(0)"],
    )

    state = engine.run(definition)

    assert state.status == LoopStatus.FAILED
    assert state.action_count == 0
    assert state.stop_reason.startswith("planner_error:PlanContractError:")
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_llm_repair_contract_correction_executes_only_validated_actions(tmp_path) -> None:
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    client = SequenceJSONClient(
        [
            {
                "actions": [
                    {
                        "tool": "shell",
                        "arguments": {"command": "unsafe"},
                    }
                ]
            },
            {
                "rationale": "schema corrected",
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
                ],
            },
        ]
    )
    engine, definition = build_llm_repair_loop(
        workspace_root=tmp_path,
        goal="Set value to 2",
        llm_client=client,
        verification_command=[
            sys.executable,
            "-c",
            "from target import value; raise SystemExit(0 if value == 2 else 1)",
        ],
    )

    state = engine.run(definition)

    assert state.status == LoopStatus.COMPLETED
    assert state.iteration == 1
    assert [result.action.tool for result in state.action_results] == [
        "apply_patch",
        "run_verification",
    ]
    assert len(client.messages) == 2
    assert "value = 2" in target.read_text(encoding="utf-8")


def test_llm_repair_cli_uses_gateway_adapter_without_persisting_secret(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    fake_client = SequenceJSONClient(
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
    monkeypatch.setenv("TEST_LLM_SECRET", "do-not-persist")
    monkeypatch.setattr(
        "loop_engine.cli.OpenAICompatibleJSONClient",
        lambda **kwargs: fake_client,
    )

    exit_code = main(
        [
            "llm-repair",
            "--workspace",
            str(tmp_path),
            "--goal",
            "Set target value to 2",
            "--api-key-env",
            "TEST_LLM_SECRET",
            "--",
            sys.executable,
            "-c",
            "from target import value; raise SystemExit(0 if value == 2 else 1)",
        ]
    )

    raw_output = capsys.readouterr().out
    output = json.loads(raw_output)
    assert exit_code == 0
    assert output["status"] == "completed"
    assert output["definition"]["metadata"]["llm"]["api_key_env"] == "TEST_LLM_SECRET"
    assert "do-not-persist" not in raw_output


def test_llm_repair_cli_emits_unicode_json(tmp_path, monkeypatch, capsys) -> None:
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    fake_client = SequenceJSONClient(
        [
            {
                "rationale": "Исправить значение",
                "actions": [
                    {
                        "tool": "apply_patch",
                        "arguments": {
                            "path": "target.py",
                            "old_text": "value = 1",
                            "new_text": "value = 2",
                        },
                        "reason": "Русское описание",
                    },
                    {"tool": "run_verification", "arguments": {}},
                ],
            }
        ]
    )
    monkeypatch.setattr(
        "loop_engine.cli.OpenAICompatibleJSONClient",
        lambda **kwargs: fake_client,
    )

    exit_code = main(
        [
            "llm-repair",
            "--workspace",
            str(tmp_path),
            "--goal",
            "Исправить значение",
            "--",
            sys.executable,
            "-c",
            "from target import value; raise SystemExit(0 if value == 2 else 1)",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["latest_plan"]["rationale"] == "Исправить значение"
