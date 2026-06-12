from __future__ import annotations

import json

import pytest

from loop_engine.tasks import (
    LexicographicStrategyJudge,
    StrategyComparison,
    StrategyJudgePolicy,
    StrategyMetrics,
    StrategyObjective,
)


def _metrics(
    strategy: str,
    *,
    status: str = "completed",
    nodes: int = 1,
    leaves: int = 1,
    executions: int = 1,
    failed: int = 0,
    blocked: int = 0,
) -> StrategyMetrics:
    return StrategyMetrics(
        strategy=strategy,
        case="case",
        root_status=status,
        node_count=nodes,
        leaf_count=leaves,
        max_depth=max(0, nodes - 1),
        dependency_edge_count=max(0, leaves - 1),
        leaf_executions=executions,
        event_count=nodes + executions,
        failed_count=failed,
        blocked_count=blocked,
        topology_sha256=str(nodes) * 64,
        outcome_sha256=str(executions) * 64,
    )


def _comparison(*runs: StrategyMetrics) -> StrategyComparison:
    return StrategyComparison(
        case="case",
        runs=tuple(runs),
        topology_groups={},
        outcome_groups={},
    )


def _policy(*objectives: StrategyObjective) -> StrategyJudgePolicy:
    return StrategyJudgePolicy.create(objectives=list(objectives))


def test_lexicographic_policy_ranks_by_declared_objective_order() -> None:
    judge = LexicographicStrategyJudge(
        _policy(
            StrategyObjective("failed_count"),
            StrategyObjective("leaf_executions"),
            StrategyObjective("node_count"),
        )
    )
    comparison = _comparison(
        _metrics("compact", nodes=2, executions=2),
        _metrics("cheap", nodes=5, executions=1),
        _metrics("failed", nodes=1, executions=1, failed=1),
    )

    ranking = judge.rank(comparison)

    assert [entry.strategy for entry in ranking.entries] == [
        "cheap",
        "compact",
        "failed",
    ]
    assert [entry.rank for entry in ranking.entries] == [1, 2, 3]
    assert ranking.winners == ("cheap",)


def test_max_direction_reverses_only_declared_metric() -> None:
    ranking = LexicographicStrategyJudge(
        _policy(StrategyObjective("leaf_count", direction="max"))
    ).rank(
        _comparison(
            _metrics("broad", leaves=3),
            _metrics("narrow", leaves=1),
        )
    )

    assert ranking.winners == ("broad",)


def test_equal_objective_tuples_share_rank() -> None:
    ranking = LexicographicStrategyJudge(
        _policy(StrategyObjective("leaf_executions"))
    ).rank(
        _comparison(
            _metrics("zeta", executions=1),
            _metrics("alpha", executions=1),
            _metrics("later", executions=2),
        )
    )

    assert [(entry.strategy, entry.rank) for entry in ranking.entries] == [
        ("alpha", 1),
        ("zeta", 1),
        ("later", 3),
    ]
    assert ranking.winners == ("alpha", "zeta")


def test_ineligible_root_status_is_unranked() -> None:
    ranking = LexicographicStrategyJudge(
        _policy(StrategyObjective("node_count"))
    ).rank(
        _comparison(
            _metrics("blocked", status="blocked", nodes=1),
            _metrics("completed", status="completed", nodes=3),
        )
    )

    assert ranking.entries[0].strategy == "completed"
    assert ranking.entries[0].rank == 1
    assert ranking.entries[1].strategy == "blocked"
    assert ranking.entries[1].rank is None
    assert ranking.entries[1].reason == "ineligible_root_status:blocked"


def test_custom_eligibility_is_external_policy() -> None:
    policy = StrategyJudgePolicy.create(
        eligible_root_statuses={"completed", "blocked"},
        objectives=[StrategyObjective("blocked_count")],
    )
    ranking = LexicographicStrategyJudge(policy).rank(
        _comparison(
            _metrics("blocked", status="blocked", blocked=1),
            _metrics("completed"),
        )
    )

    assert ranking.winners == ("completed",)
    assert all(entry.eligible for entry in ranking.entries)


def test_ranking_report_is_versioned_and_atomic(tmp_path) -> None:
    ranking = LexicographicStrategyJudge(
        _policy(StrategyObjective("node_count"))
    ).rank(_comparison(_metrics("atomic")))
    path = tmp_path / "ranking.json"

    ranking.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["winners"] == ["atomic"]
    assert payload["policy"]["objectives"] == [
        {"metric": "node_count", "direction": "min"}
    ]


def test_policy_and_comparison_validation_fail_closed() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        StrategyObjective("latency_ms")
    with pytest.raises(ValueError, match="must be unique"):
        _policy(
            StrategyObjective("node_count"),
            StrategyObjective("node_count", direction="max"),
        )
    with pytest.raises(ValueError, match="no runs"):
        LexicographicStrategyJudge(
            _policy(StrategyObjective("node_count"))
        ).rank(_comparison())
    with pytest.raises(ValueError, match="names must be unique"):
        LexicographicStrategyJudge(
            _policy(StrategyObjective("node_count"))
        ).rank(
            _comparison(
                _metrics("same"),
                _metrics("same", nodes=2),
            )
        )
    mismatched = _metrics("other")
    object.__setattr__(mismatched, "case", "different")
    with pytest.raises(ValueError, match="case mismatch"):
        LexicographicStrategyJudge(
            _policy(StrategyObjective("node_count"))
        ).rank(_comparison(mismatched))


def test_direct_policy_construction_cannot_bypass_validation() -> None:
    with pytest.raises(ValueError, match="root statuses"):
        StrategyJudgePolicy(
            eligible_root_statuses=frozenset(),
            objectives=(StrategyObjective("node_count"),),
        )
    with pytest.raises(TypeError, match="StrategyObjective"):
        StrategyJudgePolicy(
            eligible_root_statuses=frozenset({"completed"}),
            objectives=("node_count",),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="unsupported eligible"):
        StrategyJudgePolicy.create(
            eligible_root_statuses={"complete"},
            objectives=[StrategyObjective("node_count")],
        )
