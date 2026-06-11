from __future__ import annotations

import json

import pytest

from loop_engine import LoopDefinition, LoopState
from loop_engine.adapters import (
    OpenAICompatibleJSONClient,
    ValidatedLLMPlanner,
    parse_json_object,
)


class StaticJSONClient:
    def __init__(self, payload):
        self.payload = payload
        self.messages = []

    def complete_json(self, messages):
        self.messages.append(messages)
        return self.payload


def _state() -> LoopState:
    return LoopState(run_id="llm-plan", definition=LoopDefinition(goal="repair target.py"))


def test_validated_llm_planner_builds_plan_from_allowed_json() -> None:
    client = StaticJSONClient(
        {
            "rationale": "inspect target",
            "actions": [
                {
                    "tool": "read_text",
                    "arguments": {"path": "target.py"},
                    "reason": "read before editing",
                }
            ],
            "expected_evidence": ["file content"],
        }
    )

    plan = ValidatedLLMPlanner(client).plan(_state())

    assert plan.actions[0].tool == "read_text"
    assert plan.actions[0].arguments == {"path": "target.py"}
    system_prompt = client.messages[0][0]["content"]
    assert "Never claim completion" in system_prompt


def test_validated_llm_planner_accepts_legacy_response_wrapper() -> None:
    client = StaticJSONClient(
        {
            "response": {
                "actions": [
                    {"tool": "read_text", "arguments": {"path": "target.py"}}
                ]
            }
        }
    )

    plan = ValidatedLLMPlanner(client).plan(_state())

    assert plan.actions[0].tool == "read_text"


def test_validated_llm_planner_ignores_known_prompt_echo_fields() -> None:
    client = StaticJSONClient(
        {
            "rationale": "inspect",
            "actions": [{"tool": "read_text", "arguments": {"path": "target.py"}}],
            "capabilities": {"read_text": {"path": "relative file"}},
            "rules": ["Use only listed capabilities."],
        }
    )

    plan = ValidatedLLMPlanner(client).plan(_state())

    assert plan.actions[0].tool == "read_text"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"actions": [{"tool": "shell", "arguments": {}}]},
            "unknown tool",
        ),
        (
            {"actions": [{"tool": "read_text", "arguments": {"path": "../secret"}}]},
            "workspace-relative",
        ),
        (
            {
                "actions": [
                    {
                        "tool": "run_verification",
                        "arguments": {"command": "arbitrary"},
                    }
                ]
            },
            "unknown arguments",
        ),
        (
            {
                "actions": [
                    {
                        "tool": "apply_patch",
                        "arguments": {
                            "path": "target.py",
                            "old_text": "",
                            "new_text": "x",
                        },
                    }
                ]
            },
            "old_text",
        ),
    ],
)
def test_validated_llm_planner_rejects_unsafe_plan(payload, message) -> None:
    with pytest.raises(ValueError, match=message):
        ValidatedLLMPlanner(StaticJSONClient(payload)).plan(_state())


def test_llm_context_bounds_large_tool_output() -> None:
    state = _state()
    from loop_engine import Action, ActionResult

    state.action_results.append(
        ActionResult(
            action=Action(tool="read_text", arguments={"path": "large.py"}),
            status="ok",
            output={"text": "x" * 10_000},
        )
    )
    client = StaticJSONClient(
        {
            "actions": [
                {"tool": "search_text", "arguments": {"query": "needle"}}
            ]
        }
    )

    ValidatedLLMPlanner(client, max_result_chars=100).plan(state)

    context = json.loads(client.messages[0][1]["content"])
    assert "truncated_json" in context["recent_results"][0]["output"]


def test_parse_json_object_accepts_single_json_fence() -> None:
    assert parse_json_object('```json\n{"actions": []}\n```') == {"actions": []}


def test_parse_json_object_rejects_prose() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_json_object('Here is the plan: {"actions": []}')


def test_openai_compatible_client_uses_gateway_contract(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"actions":[{"tool":"run_verification","arguments":{}}]}'
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("loop_engine.adapters.llm_http.urlopen", fake_urlopen)
    client = OpenAICompatibleJSONClient(
        base_url="http://127.0.0.1:8000",
        model="auto",
        timeout_seconds=12,
    )

    result = client.complete_json([{"role": "user", "content": "plan"}])

    assert captured["url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert captured["payload"]["model"] == "auto"
    assert captured["payload"]["stream"] is False
    assert captured["timeout"] == 12
    assert result["actions"][0]["tool"] == "run_verification"
