"""Versioned report contracts for the consolidation benchmark."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from ..tasks import StrategyComparison, StrategyRanking

CONSOLIDATION_BENCHMARK_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class BenchmarkAcceptanceCheck:
    name: str
    passed: bool
    details: str


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    benchmark: str
    comparison: StrategyComparison
    ranking: StrategyRanking
    checks: tuple[BenchmarkAcceptanceCheck, ...]

    def __post_init__(self) -> None:
        benchmark = self.benchmark.strip()
        if not benchmark:
            raise ValueError("benchmark name is required")
        if self.comparison.case != self.ranking.case:
            raise ValueError("benchmark comparison and ranking case differ")
        object.__setattr__(self, "benchmark", benchmark)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_dict(self) -> dict:
        return {
            "schema_version": CONSOLIDATION_BENCHMARK_SCHEMA_VERSION,
            "benchmark": self.benchmark,
            "passed": self.passed,
            "checks": [asdict(check) for check in self.checks],
            "comparison": self.comparison.to_dict(),
            "ranking": self.ranking.to_dict(),
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


ConsolidationBenchmarkReport = BenchmarkReport
