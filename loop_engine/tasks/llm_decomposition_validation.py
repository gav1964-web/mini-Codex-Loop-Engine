"""Deterministic validation for untrusted LLM task decomposition."""

from __future__ import annotations

import json
import re
from typing import Any

from .models import AtomicLeafSpec, AtomicityDecision, ChildTaskSpec

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class TaskDecompositionValidator:
    def __init__(self, *, max_children: int) -> None:
        self.max_children = max_children

    def validate(self, payload: dict[str, Any]) -> AtomicityDecision:
        if set(payload) == {"response"} and isinstance(payload["response"], dict):
            payload = dict(payload["response"])
        if len(payload) == 1:
            wrapper = next(iter(payload))
            if wrapper in {"atomic", "decompose"} and isinstance(
                payload[wrapper],
                dict,
            ):
                payload = dict(payload[wrapper])
        decision = payload.get("decision")
        if decision == "atomic":
            self._require_exact_fields(payload, {"decision", "reason", "leaf"})
            return AtomicityDecision(
                is_atomic=True,
                reason=self._short_string(payload.get("reason"), "reason", 2000),
                leaf=self._validate_leaf(payload.get("leaf")),
            )
        if decision == "decompose":
            self._require_exact_fields(payload, {"decision", "reason", "children"})
            raw_children = payload.get("children")
            if not isinstance(raw_children, list) or not raw_children:
                raise ValueError("children must be a non-empty array")
            if len(raw_children) > self.max_children:
                raise ValueError("decomposition exceeds max_children")
            children = [self._validate_child(item) for item in raw_children]
            self._validate_dependencies(children)
            return AtomicityDecision(
                is_atomic=False,
                reason=self._short_string(payload.get("reason"), "reason", 2000),
                children=children,
            )
        raise ValueError("decision must be atomic or decompose")

    def _validate_leaf(self, value: Any) -> AtomicLeafSpec:
        if not isinstance(value, dict):
            raise ValueError("leaf must be a JSON object")
        self._require_exact_fields(
            value,
            {"goal", "success_criteria", "required_capabilities", "metadata"},
        )
        return AtomicLeafSpec(
            goal=self._required_string(value.get("goal"), "leaf goal", 4000),
            success_criteria=self._string_list(
                value.get("success_criteria"),
                "leaf success_criteria",
                required=True,
            ),
            required_capabilities=self._capability_list(
                value.get("required_capabilities"),
                required=True,
            ),
            metadata=self._metadata(value.get("metadata")),
        )

    def _validate_child(self, value: Any) -> ChildTaskSpec:
        if not isinstance(value, dict):
            raise ValueError("each child must be a JSON object")
        self._require_exact_fields(
            value,
            {
                "key",
                "goal",
                "success_criteria",
                "required_capabilities",
                "depends_on",
                "metadata",
            },
        )
        key = self._required_string(value.get("key"), "child key", 40)
        if not _KEY_PATTERN.fullmatch(key):
            raise ValueError(f"invalid child key: {key}")
        return ChildTaskSpec(
            key=key,
            goal=self._required_string(value.get("goal"), "child goal", 4000),
            success_criteria=self._string_list(
                value.get("success_criteria"),
                "child success_criteria",
            ),
            required_capabilities=self._capability_list(
                value.get("required_capabilities"),
            ),
            depends_on=self._string_list(
                value.get("depends_on"),
                "child depends_on",
            ),
            metadata=self._metadata(value.get("metadata")),
        )

    @staticmethod
    def _validate_dependencies(children: list[ChildTaskSpec]) -> None:
        keys = [child.key for child in children]
        if len(keys) != len(set(keys)):
            raise ValueError("child keys must be unique")
        key_set = set(keys)
        dependencies = {child.key: child.depends_on for child in children}
        for child in children:
            unknown = set(child.depends_on) - key_set
            if unknown:
                raise ValueError(
                    f"unknown dependencies for {child.key}: {sorted(unknown)}"
                )
            if child.key in child.depends_on:
                raise ValueError(f"child {child.key} cannot depend on itself")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("child dependencies contain a cycle")
            if key in visited:
                return
            visiting.add(key)
            for dependency in dependencies[key]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in keys:
            visit(key)

    @staticmethod
    def _require_exact_fields(value: dict[str, Any], expected: set[str]) -> None:
        unknown = set(value) - expected
        missing = expected - set(value)
        if unknown:
            raise ValueError(f"unknown decomposition fields: {sorted(unknown)}")
        if missing:
            raise ValueError(f"missing decomposition fields: {sorted(missing)}")

    @staticmethod
    def _short_string(value: Any, name: str, limit: int) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        if len(value) > limit:
            raise ValueError(f"{name} exceeds {limit} characters")
        return value

    @classmethod
    def _required_string(cls, value: Any, name: str, limit: int) -> str:
        result = cls._short_string(value, name, limit).strip()
        if not result:
            raise ValueError(f"{name} must be non-empty")
        return result

    @classmethod
    def _string_list(
        cls,
        value: Any,
        name: str,
        *,
        required: bool = False,
    ) -> list[str]:
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValueError(f"{name} must be an array of strings")
        if required and not value:
            raise ValueError(f"{name} must be non-empty")
        if len(value) > 16:
            raise ValueError(f"{name} is too large")
        return [
            cls._required_string(item, f"{name} item", 1000)
            for item in value
        ]

    @classmethod
    def _capability_list(
        cls,
        value: Any,
        *,
        required: bool = False,
    ) -> list[str]:
        capabilities = cls._string_list(
            value,
            "required_capabilities",
            required=required,
        )
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("required_capabilities must be unique")
        invalid = [
            capability
            for capability in capabilities
            if not _CAPABILITY_PATTERN.fullmatch(capability)
        ]
        if invalid:
            raise ValueError(f"invalid capability names: {invalid}")
        return capabilities

    @staticmethod
    def _metadata(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("metadata must be a JSON object")
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        if len(encoded) > 4000:
            raise ValueError("metadata exceeds 4000 characters")
        return dict(value)
