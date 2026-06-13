"""Bounded typed selector expressions for parent integration routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import TaskNode

_MAX_SELECTOR_GROUP_DEPTH = 4
_MAX_SELECTOR_NODES = 16


@dataclass(frozen=True, slots=True)
class IntegrationSelector:
    kind: str
    value: str | int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str):
            raise TypeError("integration selector kind must be a string")
        kind = self.kind.strip()
        if kind not in {
            "node_id_prefix",
            "depth",
            "required_capability",
        }:
            raise ValueError(f"unsupported integration selector kind: {kind}")
        value: str | int = self.value
        if kind == "depth":
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("integration depth selector must be non-negative")
        else:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("integration selector string value is required")
            value = value.strip()
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", value)

    @classmethod
    def node_id_prefix(cls, prefix: str) -> IntegrationSelector:
        return cls(kind="node_id_prefix", value=prefix)

    @classmethod
    def depth(cls, depth: int) -> IntegrationSelector:
        return cls(kind="depth", value=depth)

    @classmethod
    def required_capability(cls, capability: str) -> IntegrationSelector:
        return cls(kind="required_capability", value=capability)

    def matches(self, node: TaskNode) -> bool:
        if self.kind == "node_id_prefix":
            return node.id.startswith(str(self.value))
        if self.kind == "depth":
            return node.depth == self.value
        if self.kind == "required_capability":
            return self.value in node.required_capabilities
        raise AssertionError(f"unhandled integration selector kind: {self.kind}")

    def to_dict(self) -> dict[str, str | int]:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True, slots=True)
class IntegrationSelectorGroup:
    operator: str
    selectors: tuple[IntegrationSelector | IntegrationSelectorGroup, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.operator, str):
            raise TypeError("integration selector group operator must be a string")
        operator = self.operator.strip()
        if operator not in {"all", "any"}:
            raise ValueError(
                f"unsupported integration selector group operator: {operator}"
            )
        selectors = tuple(self.selectors)
        if not selectors:
            raise ValueError("integration selector group must be non-empty")
        if any(
            not isinstance(selector, (IntegrationSelector, IntegrationSelectorGroup))
            for selector in selectors
        ):
            raise TypeError(
                "integration selector group must contain typed selectors"
            )
        depth, node_count = _selector_shape(selectors)
        if depth > _MAX_SELECTOR_GROUP_DEPTH:
            raise ValueError(
                "integration selector group exceeds maximum depth "
                f"{_MAX_SELECTOR_GROUP_DEPTH}"
            )
        if node_count > _MAX_SELECTOR_NODES:
            raise ValueError(
                "integration selector group exceeds maximum node count "
                f"{_MAX_SELECTOR_NODES}"
            )
        object.__setattr__(self, "operator", operator)
        object.__setattr__(self, "selectors", selectors)

    @classmethod
    def all_of(
        cls,
        selectors: list[IntegrationSelector | IntegrationSelectorGroup]
        | tuple[IntegrationSelector | IntegrationSelectorGroup, ...],
    ) -> IntegrationSelectorGroup:
        return cls(operator="all", selectors=tuple(selectors))

    @classmethod
    def any_of(
        cls,
        selectors: list[IntegrationSelector | IntegrationSelectorGroup]
        | tuple[IntegrationSelector | IntegrationSelectorGroup, ...],
    ) -> IntegrationSelectorGroup:
        return cls(operator="any", selectors=tuple(selectors))

    def matches(self, node: TaskNode) -> bool:
        matches = (selector.matches(node) for selector in self.selectors)
        return all(matches) if self.operator == "all" else any(matches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "selectors": [selector.to_dict() for selector in self.selectors],
        }


IntegrationSelectorExpression = IntegrationSelector | IntegrationSelectorGroup


def _selector_shape(
    selectors: tuple[IntegrationSelectorExpression, ...],
) -> tuple[int, int]:
    depths = [
        0 if isinstance(selector, IntegrationSelector) else 1 + _group_depth(selector)
        for selector in selectors
    ]
    node_count = 1 + sum(_selector_node_count(selector) for selector in selectors)
    return max(depths, default=0) + 1, node_count


def _group_depth(group: IntegrationSelectorGroup) -> int:
    return max(
        (
            0
            if isinstance(selector, IntegrationSelector)
            else 1 + _group_depth(selector)
        )
        for selector in group.selectors
    )


def _selector_node_count(selector: IntegrationSelectorExpression) -> int:
    if isinstance(selector, IntegrationSelector):
        return 1
    return 1 + sum(_selector_node_count(child) for child in selector.selectors)
