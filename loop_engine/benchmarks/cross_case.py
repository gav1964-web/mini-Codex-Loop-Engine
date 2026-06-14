"""Cross-case strategy profiles based only on explicit strategy roles."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .cross_case_models import (
    CaseRoleResult,
    CrossCaseProfilePolicy,
    CrossCaseProfileReport,
    StrategyRoleProfile,
)
from .history_models import BenchmarkConfidenceReport


class CrossCaseProfileAnalyzer:
    def __init__(self, policy: CrossCaseProfilePolicy) -> None:
        self.policy = policy

    def analyze(
        self,
        reports: tuple[BenchmarkConfidenceReport, ...],
    ) -> CrossCaseProfileReport:
        if len(reports) < self.policy.minimum_cases:
            raise ValueError("cross-case profile has insufficient cases")
        cases = tuple(sorted(report.case for report in reports))
        if len(cases) != len(set(cases)):
            raise ValueError("cross-case confidence cases must be unique")
        expected = set(self.policy.role_mappings)
        if set(cases) != expected:
            raise ValueError("cross-case reports do not match role mappings")

        case_results = tuple(
            self._case_result(
                next(report for report in reports if report.case == case)
            )
            for case in cases
        )
        roles = tuple(sorted(case_results[0].role_order))
        if any(tuple(sorted(item.role_order)) != roles for item in case_results):
            raise ValueError("cross-case cases must expose the same roles")
        profiles = tuple(
            _role_profile(role, case_results)
            for role in roles
        )
        best_ordinal = min(item.ordinal_sum for item in profiles)
        consensus = tuple(
            item.role for item in profiles if item.ordinal_sum == best_ordinal
        )
        winner_share = max(
            item.win_share_basis_points
            for item in profiles
            if item.role in consensus
        )
        status, reason = _profile_status(
            case_results,
            consensus,
            winner_share,
            self.policy,
        )
        return CrossCaseProfileReport(
            status=status,
            cases=case_results,
            profiles=tuple(
                sorted(
                    profiles,
                    key=lambda item: (
                        item.ordinal_sum,
                        -item.case_wins,
                        item.role,
                    ),
                )
            ),
            consensus_roles=consensus,
            winner_share_basis_points=winner_share,
            policy=self.policy,
            reason=reason,
        )

    def _case_result(
        self,
        report: BenchmarkConfidenceReport,
    ) -> CaseRoleResult:
        mapping = self.policy.role_mappings[report.case]
        strategies = tuple(item.strategy for item in report.strategies)
        if set(strategies) != set(mapping):
            raise ValueError(
                f"cross-case strategy mapping mismatch:{report.case}"
            )
        return CaseRoleResult(
            case=report.case,
            source_status=report.status,
            winning_roles=tuple(
                sorted(mapping[strategy] for strategy in report.consensus_winners)
            ),
            role_order=tuple(mapping[strategy] for strategy in strategies),
        )


def load_benchmark_confidence(path: str | Path) -> BenchmarkConfidenceReport:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return BenchmarkConfidenceReport.from_dict(dict(payload))


def write_cross_case_profile(
    path: str | Path,
    report: CrossCaseProfileReport,
) -> Path:
    if not isinstance(report, CrossCaseProfileReport):
        raise TypeError("cross-case writer requires profile report")
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def _role_profile(
    role: str,
    cases: tuple[CaseRoleResult, ...],
) -> StrategyRoleProfile:
    ordinals = [
        case.role_order.index(role) + 1
        for case in cases
    ]
    wins = sum(
        role in case.winning_roles and len(case.winning_roles) == 1
        for case in cases
    )
    return StrategyRoleProfile(
        role=role,
        case_wins=wins,
        win_share_basis_points=round(wins * 10000 / len(cases)),
        ordinal_sum=sum(ordinals),
        average_ordinal_millis=round(sum(ordinals) * 1000 / len(ordinals)),
    )


def _profile_status(
    cases: tuple[CaseRoleResult, ...],
    consensus: tuple[str, ...],
    winner_share: int,
    policy: CrossCaseProfilePolicy,
) -> tuple[str, str]:
    if any(case.source_status != "confident" for case in cases):
        return "low_confidence", "one or more source cases are not confident"
    if len(consensus) != 1:
        return "low_confidence", "cross-case role consensus is tied"
    if winner_share < policy.minimum_winner_share_basis_points:
        return "low_confidence", "role case-win share is below policy"
    return "confident", "unique role consensus meets cross-case policy"
