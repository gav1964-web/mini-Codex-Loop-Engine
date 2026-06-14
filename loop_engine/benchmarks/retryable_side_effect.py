"""Benchmark bounded retry of an idempotent side-effecting leaf."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from ..tasks import (
    ChildTaskSpec,
    DecompositionStrategyRunner,
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    LeafExecutionResult,
    LexicographicStrategyJudge,
    ReplayTaskCase,
    ResourceClaim,
    ScriptedTaskDecomposer,
    StrategyJudgePolicy,
    StrategyObjective,
    StrategySamplingPolicy,
    StrategyUsage,
    TaskBudget,
    TaskRetryPolicy,
    TaskScheduler,
    TaskSchedulerPolicy,
)
from .models import BenchmarkAcceptanceCheck, BenchmarkReport
from .retry_workspace import RetryAudit, RetryWorkspace

_IDEMPOTENCY_KEY = "commit-result-v1"
_CAPABILITIES = {
    "retry.full",
    "retry.inspect",
    "retry.prepare",
    "retry.commit",
}


class _NamedDecomposer(ScriptedTaskDecomposer):
    def __init__(self, strategy: str, decompositions) -> None:
        super().__init__(decompositions)
        self.strategy = strategy


class _RetryUsage:
    def measure(self, *, strategy, case, graph) -> StrategyUsage:
        estimates = {
            "monolithic": (850, 170, 850),
            "parallel_retry": (620, 125, 620),
            "sequential_retry": (620, 125, 620),
        }
        input_tokens, output_tokens, cost = estimates[strategy]
        return StrategyUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microunits=cost,
            cost_basis="benchmark-estimate-v1",
        )


def run_retryable_side_effect_benchmark(
    output_path: str | Path = "build/retryable_side_effect/report.json",
    *,
    sample_count: int = 3,
    operation_delay_seconds: float = 0.04,
) -> BenchmarkReport:
    audits: list[RetryAudit] = []
    work_roots: list[Path] = []

    def scheduler_factory(decomposer):
        strategy = decomposer.strategy
        root = Path(tempfile.mkdtemp(prefix=f"retry-{strategy}-"))
        work_roots.append(root)
        workspace = RetryWorkspace(
            root,
            strategy=strategy,
            operation_delay_seconds=operation_delay_seconds,
            idempotency_key=_IDEMPOTENCY_KEY,
        )

        def retry_failure() -> LeafExecutionResult:
            return LeafExecutionResult(
                status="failed",
                summary="transient commit failure",
                error="transient_io",
                retryable=True,
                retry_code="transient_io",
                idempotency_key=_IDEMPOTENCY_KEY,
            )

        def execute(node, graph):
            capability = node.required_capabilities[0]
            if capability == "retry.full":
                success, evidence = workspace.full()
                if not success:
                    return retry_failure()
                audits.append(workspace.audit(verified=evidence["passed"]))
                return _completed("full retry workflow completed", evidence)
            if capability == "retry.commit":
                success, evidence = workspace.commit()
                return (
                    _completed("idempotent commit completed", evidence)
                    if success
                    else retry_failure()
                )
            operations = {
                "retry.inspect": workspace.inspect,
                "retry.prepare": workspace.prepare,
            }
            return _completed(
                f"{capability} completed",
                operations[capability](),
            )

        def integrate(node, graph):
            verification = workspace.verify()
            audits.append(workspace.audit(verified=verification["passed"]))
            return _completed(
                "retried side effect verified",
                verification,
            )

        return TaskScheduler(
            decomposer=decomposer,
            capability_resolver=InMemoryCapabilityResolver(_CAPABILITIES),
            leaf_executor=FunctionLeafExecutor(execute),
            integration_verifier=FunctionIntegrationVerifier(integrate),
            policy=_scheduler_policy(root),
            retry_policy=_retry_policy(),
        )

    try:
        comparison = DecompositionStrategyRunner(
            scheduler_factory,
            usage_provider=_RetryUsage(),
            sampling_policy=StrategySamplingPolicy(sample_count=sample_count),
        ).compare(_case(), _strategies())
        ranking = LexicographicStrategyJudge(_judge_policy()).rank(comparison)
        report = BenchmarkReport(
            benchmark="retryable-idempotent-side-effect",
            comparison=comparison,
            ranking=ranking,
            checks=_acceptance_checks(
                comparison,
                ranking.winners,
                audits,
                sample_count,
            ),
        )
        report.save(output_path)
        return report
    finally:
        for root in work_roots:
            shutil.rmtree(root, ignore_errors=True)


def _case() -> ReplayTaskCase:
    return ReplayTaskCase(
        name="retryable-idempotent-side-effect",
        goal="Retry one transient commit without duplicating its side effect",
        success_criteria=(
            "transient failure is retried within budget",
            "the same authorized idempotency key is used",
            "the side effect is committed exactly once",
        ),
        required_capabilities=("retry.full",),
        budget=TaskBudget(max_nodes=6, max_depth=2, max_leaf_executions=6),
    )


def _strategies():
    inspect = ChildTaskSpec(
        key="inspect",
        goal="Inspect target",
        required_capabilities=["retry.inspect"],
    )
    prepare = ChildTaskSpec(
        key="prepare",
        goal="Prepare commit",
        required_capabilities=["retry.prepare"],
    )
    commit = ChildTaskSpec(
        key="commit",
        goal="Commit with bounded retry",
        required_capabilities=["retry.commit"],
        depends_on=["inspect", "prepare"],
    )
    sequential_prepare = ChildTaskSpec(
        key="prepare",
        goal=prepare.goal,
        required_capabilities=prepare.required_capabilities,
        depends_on=["inspect"],
    )
    return {
        "monolithic": lambda: _NamedDecomposer("monolithic", {}),
        "parallel_retry": lambda: _NamedDecomposer(
            "parallel_retry",
            {"root": [inspect, prepare, commit]},
        ),
        "sequential_retry": lambda: _NamedDecomposer(
            "sequential_retry",
            {"root": [inspect, sequential_prepare, commit]},
        ),
    }


def _scheduler_policy(root: Path) -> TaskSchedulerPolicy:
    target = root / "result.txt"
    return TaskSchedulerPolicy.create(
        max_parallel_leaves=2,
        parallel_safe_capabilities=_CAPABILITIES,
        mutation_capabilities={"retry.full", "retry.commit"},
        resource_claims={
            "root": [ResourceClaim.workspace(target, mode="write")],
            "root.inspect": [
                ResourceClaim.create("evidence:retry-target", mode="read")
            ],
            "root.prepare": [
                ResourceClaim.create("evidence:retry-plan", mode="read")
            ],
            "root.commit": [ResourceClaim.workspace(target, mode="write")],
        },
    )


def _retry_policy() -> TaskRetryPolicy:
    return TaskRetryPolicy.create(
        max_attempts_per_leaf=2,
        retryable_codes={"transient_io"},
        idempotency_keys={
            "root": _IDEMPOTENCY_KEY,
            "root.commit": _IDEMPOTENCY_KEY,
        },
    )


def _judge_policy() -> StrategyJudgePolicy:
    return StrategyJudgePolicy.create(
        objectives=[
            StrategyObjective("failed_count"),
            StrategyObjective("blocked_count"),
            StrategyObjective("cost_microunits"),
            StrategyObjective("elapsed_ms"),
            StrategyObjective("leaf_executions"),
        ]
    )


def _completed(summary: str, evidence: dict) -> LeafExecutionResult:
    return LeafExecutionResult(
        status="completed",
        summary=summary,
        evidence=evidence,
    )


def _acceptance_checks(
    comparison,
    winners: tuple[str, ...],
    audits: list[RetryAudit],
    sample_count: int,
) -> tuple[BenchmarkAcceptanceCheck, ...]:
    by_strategy = {
        strategy: [audit for audit in audits if audit.strategy == strategy]
        for strategy in _strategies()
    }
    coverage = all(
        len(strategy_audits) == sample_count
        for strategy_audits in by_strategy.values()
    )
    return (
        BenchmarkAcceptanceCheck(
            "all_strategies_completed",
            coverage
            and all(run.root_status == "completed" for run in comparison.runs)
            and all(audit.verified for audit in audits),
            "every strategy completed after one authorized retry",
        ),
        BenchmarkAcceptanceCheck(
            "single_transient_failure",
            coverage
            and all(audit.transient_failures == 1 for audit in audits),
            "each isolated run observed one transient failure",
        ),
        BenchmarkAcceptanceCheck(
            "side_effect_exactly_once",
            coverage
            and all(audit.side_effect_count == 1 for audit in audits),
            "the committed side effect was materialized exactly once",
        ),
        BenchmarkAcceptanceCheck(
            "idempotency_key_stable",
            coverage
            and all(
                audit.idempotency_keys
                == (_IDEMPOTENCY_KEY, _IDEMPOTENCY_KEY)
                for audit in audits
            ),
            "both attempts used the externally authorized idempotency key",
        ),
        BenchmarkAcceptanceCheck(
            "independent_work_overlapped",
            coverage
            and all(
                audit.independent_overlap
                for audit in by_strategy["parallel_retry"]
            ),
            "parallel retry overlapped independent inspect and prepare",
        ),
        BenchmarkAcceptanceCheck(
            "parallel_strategy_ranked_first",
            winners == ("parallel_retry",),
            f"ranking winners: {','.join(winners)}",
        ),
    )
