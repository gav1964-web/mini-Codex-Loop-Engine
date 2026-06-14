"""Confidence-aware consensus over compatible benchmark snapshots."""

from __future__ import annotations

import json
import os
import statistics
from pathlib import Path

from .history_models import (
    BenchmarkConfidencePolicy,
    BenchmarkConfidenceReport,
    BenchmarkHistoryEntry,
    StrategyConfidence,
)


class BenchmarkConfidenceAnalyzer:
    def __init__(self, policy: BenchmarkConfidencePolicy | None = None) -> None:
        self.policy = policy or BenchmarkConfidencePolicy()

    def analyze(
        self,
        entries: tuple[BenchmarkHistoryEntry, ...],
    ) -> BenchmarkConfidenceReport:
        if not entries:
            raise ValueError("benchmark confidence requires history")
        ordered = tuple(
            sorted(
                entries,
                key=lambda entry: (entry.recorded_at, entry.run_id),
                reverse=True,
            )[: self.policy.history_window]
        )
        _validate_compatible_history(ordered)
        strategy_names = tuple(
            item.strategy for item in ordered[0].strategies
        )
        aggregates = tuple(
            _strategy_confidence(name, ordered, len(strategy_names))
            for name in strategy_names
        )
        best_rank_sum = min(item.rank_sum for item in aggregates)
        consensus = tuple(
            item.strategy
            for item in aggregates
            if item.rank_sum == best_rank_sum
        )
        winner_share = max(
            item.first_place_share_basis_points
            for item in aggregates
            if item.strategy in consensus
        )
        passed_count = sum(entry.passed for entry in ordered)
        status, reason = _confidence_status(
            run_count=len(ordered),
            passed_count=passed_count,
            consensus=consensus,
            winner_share=winner_share,
            policy=self.policy,
        )
        return BenchmarkConfidenceReport(
            status=status,
            benchmark=ordered[0].benchmark,
            case=ordered[0].case,
            run_ids=tuple(entry.run_id for entry in ordered),
            passed_run_count=passed_count,
            consensus_winners=consensus,
            winner_share_basis_points=winner_share,
            policy=self.policy,
            strategies=tuple(
                sorted(
                    aggregates,
                    key=lambda item: (
                        item.rank_sum,
                        -item.first_place_count,
                        item.median_elapsed_ms,
                        item.strategy,
                    ),
                )
            ),
            reason=reason,
        )


def write_benchmark_confidence(
    path: str | Path,
    report: BenchmarkConfidenceReport,
) -> Path:
    if not isinstance(report, BenchmarkConfidenceReport):
        raise TypeError("benchmark confidence writer requires report contract")
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def _strategy_confidence(
    strategy: str,
    entries: tuple[BenchmarkHistoryEntry, ...],
    strategy_count: int,
) -> StrategyConfidence:
    snapshots = [
        next(item for item in entry.strategies if item.strategy == strategy)
        for entry in entries
    ]
    ranks = [
        item.rank if item.rank is not None else strategy_count + 1
        for item in snapshots
    ]
    elapsed = [item.elapsed_ms for item in snapshots]
    first_count = sum(
        strategy in entry.winners and len(entry.winners) == 1
        for entry in entries
    )
    median = int(statistics.median(elapsed))
    return StrategyConfidence(
        strategy=strategy,
        first_place_count=first_count,
        first_place_share_basis_points=round(
            first_count * 10000 / len(entries)
        ),
        rank_sum=sum(ranks),
        average_rank_millis=round(sum(ranks) * 1000 / len(ranks)),
        median_elapsed_ms=median,
        elapsed_mad_ms=int(
            statistics.median(abs(value - median) for value in elapsed)
        ),
    )


def _confidence_status(
    *,
    run_count: int,
    passed_count: int,
    consensus: tuple[str, ...],
    winner_share: int,
    policy: BenchmarkConfidencePolicy,
) -> tuple[str, str]:
    if run_count < policy.minimum_runs:
        return "insufficient_history", "minimum benchmark run count not reached"
    if passed_count != run_count:
        return "low_confidence", "one or more benchmark runs failed acceptance"
    if len(consensus) != 1:
        return "low_confidence", "consensus ranking is tied"
    if winner_share < policy.minimum_winner_share_basis_points:
        return "low_confidence", "winner first-place share is below policy"
    return "confident", "unique consensus winner meets confidence policy"


def _validate_compatible_history(
    entries: tuple[BenchmarkHistoryEntry, ...],
) -> None:
    run_ids = [entry.run_id for entry in entries]
    if len(run_ids) != len(set(run_ids)):
        raise ValueError("benchmark history run ids must be unique")
    first = entries[0]
    identity = (
        first.benchmark,
        first.case,
        first.policy_sha256,
        tuple(item.strategy for item in first.strategies),
    )
    for entry in entries[1:]:
        current = (
            entry.benchmark,
            entry.case,
            entry.policy_sha256,
            tuple(item.strategy for item in entry.strategies),
        )
        if current != identity:
            raise ValueError("benchmark history entries are not comparable")
