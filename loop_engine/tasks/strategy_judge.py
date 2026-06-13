"""Explicit policy-driven ranking for decomposition strategy comparisons."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import TaskStatus
from .replay import StrategyComparison
from .strategy_metrics import StrategyMetrics

STRATEGY_RANKING_SCHEMA_VERSION = 1
_NUMERIC_METRICS = frozenset(
    {
        "node_count",
        "leaf_count",
        "max_depth",
        "dependency_edge_count",
        "leaf_executions",
        "event_count",
        "failed_count",
        "blocked_count",
        "elapsed_ms",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_microunits",
    }
)


@dataclass(frozen=True, slots=True)
class StrategyObjective:
    metric: str
    direction: str = "min"

    def __post_init__(self) -> None:
        if not isinstance(self.metric, str) or not isinstance(
            self.direction,
            str,
        ):
            raise TypeError("strategy objective fields must be strings")
        metric = self.metric.strip()
        direction = self.direction.strip().lower()
        if metric not in _NUMERIC_METRICS:
            raise ValueError(f"unsupported strategy objective metric: {metric}")
        if direction not in {"min", "max"}:
            raise ValueError("strategy objective direction must be min or max")
        object.__setattr__(self, "metric", metric)
        object.__setattr__(self, "direction", direction)


@dataclass(frozen=True, slots=True)
class StrategyJudgePolicy:
    eligible_root_statuses: frozenset[str]
    objectives: tuple[StrategyObjective, ...]

    def __post_init__(self) -> None:
        statuses = frozenset(status.strip() for status in self.eligible_root_statuses)
        if not statuses or "" in statuses:
            raise ValueError("eligible strategy root statuses must be non-empty")
        supported_statuses = {str(status) for status in TaskStatus}
        unknown_statuses = sorted(statuses - supported_statuses)
        if unknown_statuses:
            raise ValueError(
                f"unsupported eligible root statuses: {unknown_statuses}"
            )
        objectives = tuple(self.objectives)
        if not objectives:
            raise ValueError("at least one strategy objective is required")
        if any(not isinstance(item, StrategyObjective) for item in objectives):
            raise TypeError("strategy objectives must contain StrategyObjective")
        metrics = [item.metric for item in objectives]
        if len(metrics) != len(set(metrics)):
            raise ValueError("strategy objective metrics must be unique")
        object.__setattr__(self, "eligible_root_statuses", statuses)
        object.__setattr__(self, "objectives", objectives)

    @classmethod
    def create(
        cls,
        *,
        eligible_root_statuses: set[str] | frozenset[str] = frozenset(
            {"completed"}
        ),
        objectives: tuple[StrategyObjective, ...] | list[StrategyObjective],
    ) -> StrategyJudgePolicy:
        return cls(
            eligible_root_statuses=frozenset(eligible_root_statuses),
            objectives=tuple(objectives),
        )


@dataclass(frozen=True, slots=True)
class StrategyRank:
    strategy: str
    rank: int | None
    eligible: bool
    root_status: str
    objective_values: tuple[int, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class StrategyRanking:
    case: str
    policy: StrategyJudgePolicy
    entries: tuple[StrategyRank, ...]

    @property
    def winners(self) -> tuple[str, ...]:
        return tuple(
            entry.strategy for entry in self.entries if entry.rank == 1
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": STRATEGY_RANKING_SCHEMA_VERSION,
            "case": self.case,
            "policy": {
                "eligible_root_statuses": sorted(
                    self.policy.eligible_root_statuses
                ),
                "objectives": [
                    asdict(objective) for objective in self.policy.objectives
                ],
            },
            "winners": list(self.winners),
            "entries": [asdict(entry) for entry in self.entries],
        }

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)


class LexicographicStrategyJudge:
    """Rank measured runs using only the ordered external objective policy."""

    def __init__(self, policy: StrategyJudgePolicy) -> None:
        self.policy = policy

    def rank(self, comparison: StrategyComparison) -> StrategyRanking:
        if not comparison.runs:
            raise ValueError("strategy comparison has no runs")
        if len({run.strategy for run in comparison.runs}) != len(
            comparison.runs
        ):
            raise ValueError("strategy comparison names must be unique")
        if any(not run.strategy.strip() for run in comparison.runs):
            raise ValueError("strategy comparison names must be non-empty")
        if any(run.case != comparison.case for run in comparison.runs):
            raise ValueError("strategy comparison run case mismatch")

        self._validate_measurement_compatibility(comparison)
        eligible: list[tuple[tuple[int, ...], StrategyMetrics]] = []
        ineligible: list[StrategyRank] = []
        for run in comparison.runs:
            values = _objective_values(run, self.policy.objectives)
            if run.root_status not in self.policy.eligible_root_statuses:
                ineligible.append(
                    StrategyRank(
                        strategy=run.strategy,
                        rank=None,
                        eligible=False,
                        root_status=run.root_status,
                        objective_values=values,
                        reason=(
                            "ineligible_root_status:"
                            f"{run.root_status}"
                        ),
                    )
                )
                continue
            eligible.append((_sort_key(values, self.policy.objectives), run))

        eligible.sort(key=lambda item: (item[0], item[1].strategy))
        ranked: list[StrategyRank] = []
        previous_key: tuple[int, ...] | None = None
        current_rank = 0
        for position, (key, run) in enumerate(eligible, start=1):
            if key != previous_key:
                current_rank = position
                previous_key = key
            values = _objective_values(run, self.policy.objectives)
            ranked.append(
                StrategyRank(
                    strategy=run.strategy,
                    rank=current_rank,
                    eligible=True,
                    root_status=run.root_status,
                    objective_values=values,
                    reason=_ranking_reason(values, self.policy.objectives),
                )
            )
        entries = tuple(
            [
                *ranked,
                *sorted(ineligible, key=lambda item: item.strategy),
            ]
        )
        return StrategyRanking(
            case=comparison.case,
            policy=self.policy,
            entries=entries,
        )

    def _validate_measurement_compatibility(
        self,
        comparison: StrategyComparison,
    ) -> None:
        metrics = {objective.metric for objective in self.policy.objectives}
        if "cost_microunits" not in metrics:
            return
        bases = {
            run.cost_basis
            for run in comparison.runs
            if run.root_status in self.policy.eligible_root_statuses
        }
        if None in bases:
            raise ValueError("strategy objective cost_microunits is not measured")
        if len(bases) > 1:
            raise ValueError("strategy cost_basis values are not comparable")


def _sort_key(
    values: tuple[int, ...],
    objectives: tuple[StrategyObjective, ...],
) -> tuple[int, ...]:
    return tuple(
        value if objective.direction == "min" else -value
        for value, objective in zip(values, objectives, strict=True)
    )


def _objective_values(
    run: StrategyMetrics,
    objectives: tuple[StrategyObjective, ...],
) -> tuple[int, ...]:
    values: list[int] = []
    for objective in objectives:
        value = getattr(run, objective.metric)
        if value is None:
            raise ValueError(
                f"strategy objective {objective.metric} is not measured:"
                f"{run.strategy}"
            )
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(
                f"strategy objective {objective.metric} must be an integer"
            )
        values.append(value)
    return tuple(values)


def _ranking_reason(
    values: tuple[int, ...],
    objectives: tuple[StrategyObjective, ...],
) -> str:
    return "lexicographic:" + ",".join(
        f"{objective.metric}:{objective.direction}={value}"
        for value, objective in zip(values, objectives, strict=True)
    )
