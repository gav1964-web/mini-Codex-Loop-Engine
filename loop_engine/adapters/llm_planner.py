"""Bounded context and strict plan validation for LLM planners."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import PurePath
from typing import Any

from ..models import Action, LoopState, Plan
from ..ports import JSONLLMClient

_ACTION_ARGUMENTS = {
    "list_files": {
        "allowed": {"path", "recursive", "max_entries"},
        "required": set(),
    },
    "read_text": {
        "allowed": {"path", "max_bytes"},
        "required": {"path"},
    },
    "search_text": {
        "allowed": {"path", "query", "regex", "case_sensitive", "max_matches"},
        "required": {"query"},
    },
    "apply_patch": {
        "allowed": {
            "path",
            "old_text",
            "new_text",
            "expected_replacements",
            "expected_sha256",
        },
        "required": {"path", "old_text", "new_text"},
    },
    "run_verification": {
        "allowed": set(),
        "required": set(),
    },
}


class ValidatedLLMPlanner:
    def __init__(
        self,
        client: JSONLLMClient,
        *,
        max_actions_per_plan: int = 5,
        max_context_chars: int = 24_000,
        max_result_chars: int = 8_000,
    ) -> None:
        self.client = client
        self.max_actions_per_plan = max_actions_per_plan
        self.max_context_chars = max_context_chars
        self.max_result_chars = max_result_chars

    def plan(self, state: LoopState) -> Plan:
        payload = self.client.complete_json(self._messages(state))
        return self._validate_plan(payload)

    def _messages(self, state: LoopState) -> list[dict[str, str]]:
        contract = {
            "rationale": "short string",
            "actions": [
                {
                    "tool": "one allowed capability",
                    "arguments": {},
                    "reason": "short string",
                }
            ],
            "expected_evidence": ["short string"],
            "capabilities": {
                "list_files": {"path": "relative directory", "recursive": "bool"},
                "read_text": {"path": "relative file"},
                "search_text": {"path": "relative directory", "query": "text"},
                "apply_patch": {
                    "path": "relative existing UTF-8 file",
                    "old_text": "exact non-empty text",
                    "new_text": "replacement text",
                    "expected_replacements": "positive int, default 1",
                    "expected_sha256": "optional read_text sha256",
                },
                "run_verification": {},
            },
            "rules": [
                "Return one JSON object and no prose.",
                "Return only rationale, actions, and expected_evidence; do not repeat capabilities or rules.",
                "Use only listed capabilities and arguments.",
                "Use only workspace-relative paths.",
                f"Return at most {self.max_actions_per_plan} actions.",
                "Inspect before editing when evidence is insufficient.",
                "Run verification after a repair.",
                "Never claim completion; verifier and judge decide.",
            ],
        }
        context: dict[str, Any] = {
            "goal": state.definition.goal,
            "success_criteria": state.definition.success_criteria,
            "constraints": state.definition.constraints,
            "iteration": state.iteration,
            "current_focus": state.current_focus,
            "remaining_action_budget": max(
                0, state.definition.budget.max_actions - state.action_count
            ),
            "latest_verification": (
                asdict(state.latest_verification)
                if state.latest_verification is not None
                else None
            ),
            "recent_results": [],
            "older_results_omitted": False,
        }
        recent_results: list[dict[str, Any]] = []
        candidates = [
            {
                "tool": result.action.tool,
                "arguments": result.action.arguments,
                "status": result.status,
                "output": self._bounded_value(result.output),
                "error": result.error,
            }
            for result in state.action_results[-8:]
        ]
        for item in reversed(candidates):
            proposed = [item, *recent_results]
            context["recent_results"] = proposed
            if len(json.dumps(context, ensure_ascii=False, default=str)) > self.max_context_chars:
                context["recent_results"] = recent_results
                context["older_results_omitted"] = True
                break
            recent_results = proposed
        context_json = json.dumps(context, ensure_ascii=False, default=str)
        return [
            {
                "role": "system",
                "content": (
                    "You are a bounded coding planner. Choose the next tool actions. "
                    "You cannot execute tools or decide completion.\n"
                    + json.dumps(contract, ensure_ascii=False, indent=2)
                ),
            },
            {"role": "user", "content": context_json},
        ]

    def _validate_plan(self, payload: dict[str, Any]) -> Plan:
        if set(payload) == {"response"} and isinstance(payload["response"], dict):
            payload = dict(payload["response"])
        prompt_echo_fields = {"capabilities", "rules"}
        payload = {key: value for key, value in payload.items() if key not in prompt_echo_fields}
        allowed_top_level = {"rationale", "actions", "expected_evidence"}
        unknown = set(payload) - allowed_top_level
        if unknown:
            raise ValueError(f"unknown plan fields: {sorted(unknown)}")
        raw_actions = payload.get("actions")
        if not isinstance(raw_actions, list) or not raw_actions:
            raise ValueError("plan actions must be a non-empty array")
        if len(raw_actions) > self.max_actions_per_plan:
            raise ValueError("plan exceeds max_actions_per_plan")

        actions = [self._validate_action(item) for item in raw_actions]
        rationale = self._short_string(payload.get("rationale", ""), "rationale", 2000)
        expected = payload.get("expected_evidence", [])
        if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
            raise ValueError("expected_evidence must be an array of strings")
        if len(expected) > 20:
            raise ValueError("expected_evidence is too large")
        return Plan(
            actions=actions,
            rationale=rationale,
            expected_evidence=[self._short_string(item, "expected_evidence item", 500) for item in expected],
        )

    def _validate_action(self, value: Any) -> Action:
        if not isinstance(value, dict):
            raise ValueError("each action must be a JSON object")
        unknown_fields = set(value) - {"tool", "arguments", "reason"}
        if unknown_fields:
            raise ValueError(f"unknown action fields: {sorted(unknown_fields)}")
        tool = value.get("tool")
        if tool not in _ACTION_ARGUMENTS:
            raise ValueError(f"unknown tool: {tool}")
        arguments = value.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("action arguments must be a JSON object")
        schema = _ACTION_ARGUMENTS[tool]
        unknown_arguments = set(arguments) - schema["allowed"]
        missing_arguments = schema["required"] - set(arguments)
        if unknown_arguments:
            raise ValueError(f"unknown arguments for {tool}: {sorted(unknown_arguments)}")
        if missing_arguments:
            raise ValueError(f"missing arguments for {tool}: {sorted(missing_arguments)}")
        self._validate_arguments(tool, arguments)
        reason = self._short_string(value.get("reason", ""), "action reason", 1000)
        return Action(tool=tool, arguments=dict(arguments), reason=reason)

    def _validate_arguments(self, tool: str, arguments: dict[str, Any]) -> None:
        path = arguments.get("path")
        if path is not None:
            if not isinstance(path, str) or not path.strip():
                raise ValueError(f"{tool} path must be a non-empty string")
            pure_path = PurePath(path)
            if pure_path.is_absolute() or ".." in pure_path.parts:
                raise ValueError(f"{tool} path must be workspace-relative")
        if tool == "apply_patch":
            old_text = arguments["old_text"]
            new_text = arguments["new_text"]
            if not isinstance(old_text, str) or not old_text:
                raise ValueError("apply_patch old_text must be non-empty")
            if not isinstance(new_text, str):
                raise ValueError("apply_patch new_text must be a string")
            replacements = arguments.get("expected_replacements", 1)
            if not isinstance(replacements, int) or isinstance(replacements, bool) or replacements <= 0:
                raise ValueError("expected_replacements must be a positive integer")
        if tool == "search_text":
            if not isinstance(arguments["query"], str) or not arguments["query"]:
                raise ValueError("search_text query must be non-empty")
        for name in ("max_entries", "max_bytes", "max_matches"):
            if name in arguments:
                value = arguments[name]
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise ValueError(f"{name} must be a positive integer")
        for name in ("recursive", "regex", "case_sensitive"):
            if name in arguments and not isinstance(arguments[name], bool):
                raise ValueError(f"{name} must be a boolean")

    def _bounded_value(self, value: Any) -> Any:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        if len(encoded) <= self.max_result_chars:
            return value
        return {"truncated_json": encoded[: self.max_result_chars]}

    @staticmethod
    def _short_string(value: Any, name: str, limit: int) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        if len(value) > limit:
            raise ValueError(f"{name} exceeds {limit} characters")
        return value
