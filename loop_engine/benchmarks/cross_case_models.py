"""Contracts for strategy-role profiles across independent benchmark cases."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Mapping

CROSS_CASE_PROFILE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CrossCaseProfilePolicy:
    role_mappings: Mapping[str, Mapping[str, str]]
    minimum_cases: int = 2
    minimum_winner_share_basis_points: int = 6700

    def __post_init__(self) -> None:
        if (
            not isinstance(self.minimum_cases, int)
            or isinstance(self.minimum_cases, bool)
            or self.minimum_cases < 2
            or self.minimum_cases > 100
        ):
            raise ValueError("cross-case minimum cases must be between 2 and 100")
        share = self.minimum_winner_share_basis_points
        if (
            not isinstance(share, int)
            or isinstance(share, bool)
            or share <= 5000
            or share > 10000
        ):
            raise ValueError(
                "cross-case winner share must be between 5001 and 10000"
            )
        normalized: dict[str, dict[str, str]] = {}
        for case, mapping in self.role_mappings.items():
            case_name = case.strip()
            values = {
                strategy.strip(): role.strip()
                for strategy, role in mapping.items()
            }
            if (
                not case_name
                or not values
                or "" in values
                or "" in values.values()
                or len(values.values()) != len(set(values.values()))
            ):
                raise ValueError("cross-case role mapping is invalid")
            normalized[case_name] = values
        if len(normalized) < self.minimum_cases:
            raise ValueError("cross-case mappings do not meet minimum cases")
        object.__setattr__(
            self,
            "role_mappings",
            MappingProxyType(
                {
                    case: MappingProxyType(dict(mapping))
                    for case, mapping in normalized.items()
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class CaseRoleResult:
    case: str
    source_status: str
    winning_roles: tuple[str, ...]
    role_order: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StrategyRoleProfile:
    role: str
    case_wins: int
    win_share_basis_points: int
    ordinal_sum: int
    average_ordinal_millis: int


@dataclass(frozen=True, slots=True)
class CrossCaseProfileReport:
    status: str
    cases: tuple[CaseRoleResult, ...]
    profiles: tuple[StrategyRoleProfile, ...]
    consensus_roles: tuple[str, ...]
    winner_share_basis_points: int
    policy: CrossCaseProfilePolicy
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CROSS_CASE_PROFILE_SCHEMA_VERSION,
            "status": self.status,
            "cases": [
                {
                    **asdict(item),
                    "winning_roles": list(item.winning_roles),
                    "role_order": list(item.role_order),
                }
                for item in self.cases
            ],
            "profiles": [asdict(item) for item in self.profiles],
            "consensus_roles": list(self.consensus_roles),
            "winner_share_basis_points": self.winner_share_basis_points,
            "policy": {
                "minimum_cases": self.policy.minimum_cases,
                "minimum_winner_share_basis_points": (
                    self.policy.minimum_winner_share_basis_points
                ),
                "role_mappings": {
                    case: dict(mapping)
                    for case, mapping in self.policy.role_mappings.items()
                },
            },
            "reason": self.reason,
        }
