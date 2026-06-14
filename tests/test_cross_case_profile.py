from __future__ import annotations

from dataclasses import replace
import json
import sys

import pytest

from loop_engine.benchmarks import (
    BenchmarkConfidencePolicy,
    BenchmarkConfidenceReport,
    CrossCaseProfileAnalyzer,
    CrossCaseProfilePolicy,
    StrategyConfidence,
    load_benchmark_confidence,
    write_benchmark_confidence,
    write_cross_case_profile,
)
from tools.cross_case_profile import main as cross_case_profile_main

_MAPPINGS = {
    "python-project-change": {
        "monolithic": "monolithic",
        "sequential_staged": "sequential",
        "parallel_staged": "parallel",
    },
    "python-project-audit": {
        "monolithic": "monolithic",
        "sequential_evidence": "sequential",
        "parallel_evidence": "parallel",
    },
    "resource-contention-recovery": {
        "monolithic": "monolithic",
        "sequential_recovery": "sequential",
        "parallel_recovery": "parallel",
    },
    "retryable-idempotent-side-effect": {
        "monolithic": "monolithic",
        "sequential_retry": "sequential",
        "parallel_retry": "parallel",
    },
}


@pytest.fixture(scope="module")
def confidence_reports():
    policy = BenchmarkConfidencePolicy(minimum_runs=2)

    def confidence(case):
        by_role = {
            role: strategy
            for strategy, role in _MAPPINGS[case].items()
        }
        strategies = tuple(
            StrategyConfidence(
                strategy=by_role[role],
                first_place_count=2 if rank == 1 else 0,
                first_place_share_basis_points=10000 if rank == 1 else 0,
                rank_sum=rank * 2,
                average_rank_millis=rank * 1000,
                median_elapsed_ms=rank * 10,
                elapsed_mad_ms=0,
            )
            for rank, role in enumerate(
                ("parallel", "sequential", "monolithic"),
                start=1,
            )
        )
        return BenchmarkConfidenceReport(
            status="confident",
            benchmark=case,
            case=case,
            run_ids=(f"{case}-0", f"{case}-1"),
            passed_run_count=2,
            consensus_winners=(by_role["parallel"],),
            winner_share_basis_points=10000,
            policy=policy,
            strategies=strategies,
            reason="deterministic test confidence fixture",
        )

    return tuple(confidence(case) for case in _MAPPINGS)


def test_cross_case_profile_finds_parallel_role_consensus(
    confidence_reports,
) -> None:
    profile = CrossCaseProfileAnalyzer(
        CrossCaseProfilePolicy(role_mappings=_MAPPINGS)
    ).analyze(confidence_reports)

    assert profile.status == "confident"
    assert profile.consensus_roles == ("parallel",)
    assert profile.winner_share_basis_points == 10000
    assert profile.profiles[0].role == "parallel"
    assert profile.profiles[0].case_wins == 4
    assert {case.case for case in profile.cases} == set(_MAPPINGS)


def test_cross_case_profile_requires_confident_sources(
    confidence_reports,
) -> None:
    change, audit, recovery, retry = confidence_reports
    profile = CrossCaseProfileAnalyzer(
        CrossCaseProfilePolicy(role_mappings=_MAPPINGS)
    ).analyze(
        (
            replace(change, status="low_confidence"),
            audit,
            recovery,
            retry,
        )
    )

    assert profile.status == "low_confidence"
    assert profile.reason == "one or more source cases are not confident"


def test_cross_case_mapping_must_cover_case_strategies(
    confidence_reports,
) -> None:
    broken = {
        **_MAPPINGS,
        "python-project-audit": {
            "monolithic": "monolithic",
            "sequential_evidence": "sequential",
        },
    }

    with pytest.raises(ValueError, match="mapping mismatch"):
        CrossCaseProfileAnalyzer(
            CrossCaseProfilePolicy(role_mappings=broken)
        ).analyze(confidence_reports)


def test_cross_case_json_loading_writing_and_cli(
    tmp_path,
    confidence_reports,
    monkeypatch,
    capsys,
) -> None:
    change_path = write_benchmark_confidence(
        tmp_path / "change.json",
        confidence_reports[0],
    )
    audit_path = write_benchmark_confidence(
        tmp_path / "audit.json",
        confidence_reports[1],
    )
    assert load_benchmark_confidence(change_path) == confidence_reports[0]
    output = tmp_path / "profile.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cross_case_profile",
            "--change-confidence",
            str(change_path),
            "--audit-confidence",
            str(audit_path),
            "--recovery-confidence",
            str(
                write_benchmark_confidence(
                    tmp_path / "recovery.json",
                    confidence_reports[2],
                )
            ),
            "--output",
            str(output),
            "--retry-confidence",
            str(
                write_benchmark_confidence(
                    tmp_path / "retry.json",
                    confidence_reports[3],
                )
            ),
        ],
    )

    exit_code = cross_case_profile_main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "confident"
    assert json.loads(output.read_text(encoding="utf-8")) == payload


def test_cross_case_policy_validation_and_atomic_writer(
    tmp_path,
    confidence_reports,
) -> None:
    profile = CrossCaseProfileAnalyzer(
        CrossCaseProfilePolicy(role_mappings=_MAPPINGS)
    ).analyze(confidence_reports)
    path = write_cross_case_profile(tmp_path / "profile.json", profile)

    assert json.loads(path.read_text(encoding="utf-8")) == profile.to_dict()
    with pytest.raises(ValueError, match="minimum cases"):
        CrossCaseProfilePolicy(role_mappings=_MAPPINGS, minimum_cases=1)
    with pytest.raises(ValueError, match="winner share"):
        CrossCaseProfilePolicy(
            role_mappings=_MAPPINGS,
            minimum_winner_share_basis_points=5000,
        )
    with pytest.raises(ValueError, match="role mapping"):
        CrossCaseProfilePolicy(
            role_mappings={
                **_MAPPINGS,
                "python-project-audit": {
                    "monolithic": "same",
                    "sequential_evidence": "same",
                },
            }
        )
