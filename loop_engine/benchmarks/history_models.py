"""Contracts for benchmark history and confidence reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Any

BENCHMARK_HISTORY_SCHEMA_VERSION = 1
BENCHMARK_CONFIDENCE_SCHEMA_VERSION = 1
MAX_BENCHMARK_HISTORY_LIMIT = 100
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class BenchmarkStrategySnapshot:
    strategy: str
    rank: int | None
    eligible: bool
    elapsed_ms: int

    def __post_init__(self) -> None:
        name = self.strategy.strip()
        if not name:
            raise ValueError("benchmark strategy name is required")
        if self.rank is not None and (
            not isinstance(self.rank, int)
            or isinstance(self.rank, bool)
            or self.rank <= 0
        ):
            raise ValueError("benchmark strategy rank must be positive")
        if (
            not isinstance(self.elapsed_ms, int)
            or isinstance(self.elapsed_ms, bool)
            or self.elapsed_ms < 0
        ):
            raise ValueError("benchmark strategy elapsed_ms must be non-negative")
        if self.eligible != (self.rank is not None):
            raise ValueError("benchmark eligibility and rank must agree")
        object.__setattr__(self, "strategy", name)


@dataclass(frozen=True, slots=True)
class BenchmarkHistoryEntry:
    run_id: str
    recorded_at: float
    benchmark: str
    case: str
    passed: bool
    policy_sha256: str
    strategies: tuple[BenchmarkStrategySnapshot, ...]
    winners: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _RUN_ID_PATTERN.fullmatch(self.run_id):
            raise ValueError("benchmark history run_id is invalid")
        if not isinstance(self.recorded_at, (int, float)) or not math.isfinite(
            self.recorded_at
        ):
            raise ValueError("benchmark history recorded_at must be finite")
        if not isinstance(self.passed, bool):
            raise TypeError("benchmark history passed must be boolean")
        if not self.benchmark.strip() or not self.case.strip():
            raise ValueError("benchmark history identity fields are required")
        if not _SHA256_PATTERN.fullmatch(self.policy_sha256):
            raise ValueError("benchmark policy_sha256 is invalid")
        strategies = tuple(self.strategies)
        if not strategies:
            raise ValueError("benchmark history requires strategies")
        names = tuple(item.strategy for item in strategies)
        if len(names) != len(set(names)):
            raise ValueError("benchmark history strategies must be unique")
        winners = tuple(self.winners)
        if len(winners) != len(set(winners)) or not set(winners) <= set(names):
            raise ValueError("benchmark history winners are invalid")
        ranked_first = {
            item.strategy for item in strategies if item.rank == 1
        }
        if set(winners) != ranked_first:
            raise ValueError("benchmark winners must match first-ranked strategies")
        object.__setattr__(self, "benchmark", self.benchmark.strip())
        object.__setattr__(self, "case", self.case.strip())
        object.__setattr__(self, "strategies", strategies)
        object.__setattr__(self, "winners", winners)


@dataclass(frozen=True, slots=True)
class BenchmarkConfidencePolicy:
    history_window: int = 7
    minimum_runs: int = 3
    minimum_winner_share_basis_points: int = 6700

    def __post_init__(self) -> None:
        if (
            not isinstance(self.history_window, int)
            or isinstance(self.history_window, bool)
            or self.history_window <= 0
            or self.history_window > MAX_BENCHMARK_HISTORY_LIMIT
        ):
            raise ValueError("benchmark history window must be between 1 and 100")
        if (
            not isinstance(self.minimum_runs, int)
            or isinstance(self.minimum_runs, bool)
            or self.minimum_runs <= 0
            or self.minimum_runs > self.history_window
        ):
            raise ValueError("benchmark minimum runs must fit the history window")
        share = self.minimum_winner_share_basis_points
        if (
            not isinstance(share, int)
            or isinstance(share, bool)
            or share <= 5000
            or share > 10000
        ):
            raise ValueError(
                "benchmark winner share must be between 5001 and 10000 basis points"
            )


@dataclass(frozen=True, slots=True)
class StrategyConfidence:
    strategy: str
    first_place_count: int
    first_place_share_basis_points: int
    rank_sum: int
    average_rank_millis: int
    median_elapsed_ms: int
    elapsed_mad_ms: int


@dataclass(frozen=True, slots=True)
class BenchmarkConfidenceReport:
    status: str
    benchmark: str
    case: str
    run_ids: tuple[str, ...]
    passed_run_count: int
    consensus_winners: tuple[str, ...]
    winner_share_basis_points: int
    policy: BenchmarkConfidencePolicy
    strategies: tuple[StrategyConfidence, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": BENCHMARK_CONFIDENCE_SCHEMA_VERSION,
            "status": self.status,
            "benchmark": self.benchmark,
            "case": self.case,
            "run_ids": list(self.run_ids),
            "passed_run_count": self.passed_run_count,
            "consensus_winners": list(self.consensus_winners),
            "winner_share_basis_points": self.winner_share_basis_points,
            "policy": asdict(self.policy),
            "strategies": [asdict(item) for item in self.strategies],
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BenchmarkConfidenceReport:
        if value.get("schema_version") != BENCHMARK_CONFIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported benchmark confidence schema version")
        policy = BenchmarkConfidencePolicy(**dict(value["policy"]))
        strategies = tuple(
            StrategyConfidence(**dict(item))
            for item in value["strategies"]
        )
        report = cls(
            status=str(value["status"]),
            benchmark=str(value["benchmark"]),
            case=str(value["case"]),
            run_ids=tuple(str(item) for item in value["run_ids"]),
            passed_run_count=int(value["passed_run_count"]),
            consensus_winners=tuple(
                str(item) for item in value["consensus_winners"]
            ),
            winner_share_basis_points=int(
                value["winner_share_basis_points"]
            ),
            policy=policy,
            strategies=strategies,
            reason=str(value["reason"]),
        )
        _validate_confidence_report(report)
        return report


def _validate_confidence_report(report: BenchmarkConfidenceReport) -> None:
    if report.status not in {
        "insufficient_history",
        "low_confidence",
        "confident",
    }:
        raise ValueError("unsupported benchmark confidence status")
    if not report.benchmark.strip() or not report.case.strip():
        raise ValueError("benchmark confidence identity fields are required")
    names = tuple(item.strategy for item in report.strategies)
    if not names or len(names) != len(set(names)):
        raise ValueError("benchmark confidence strategies must be unique")
    if not set(report.consensus_winners) <= set(names):
        raise ValueError("benchmark confidence winners are invalid")
    if report.passed_run_count < 0 or report.passed_run_count > len(
        report.run_ids
    ):
        raise ValueError("benchmark confidence passed count is invalid")
