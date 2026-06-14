"""Real multi-step benchmark for decomposition strategy consolidation."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from ..tasks import (
    ChildTaskSpec,
    DecompositionStrategyRunner,
    FunctionCapabilityAcquirer,
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
    TaskScheduler,
    TaskSchedulerPolicy,
)
from .models import BenchmarkAcceptanceCheck, ConsolidationBenchmarkReport
from .workspace import BenchmarkAudit, PythonChangeWorkspace

_CAPABILITIES = {
    "project.full_change",
    "project.inspect.source",
    "project.inspect.tests",
    "project.apply",
    "project.verify",
}


class _NamedDecomposer(ScriptedTaskDecomposer):
    def __init__(self, strategy: str, decompositions) -> None:
        super().__init__(decompositions)
        self.strategy = strategy


class _BenchmarkUsage:
    def measure(self, *, strategy, case, graph) -> StrategyUsage:
        estimates = {
            "monolithic": (900, 180, 900),
            "parallel_staged": (600, 120, 600),
            "sequential_staged": (600, 120, 600),
        }
        input_tokens, output_tokens, cost = estimates[strategy]
        return StrategyUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microunits=cost,
            cost_basis="benchmark-estimate-v1",
        )


def run_consolidation_benchmark(
    output_path: str | Path = "build/consolidation_benchmark/report.json",
    *,
    sample_count: int = 3,
    read_delay_seconds: float = 0.04,
) -> ConsolidationBenchmarkReport:
    audits: list[BenchmarkAudit] = []
    work_roots: list[Path] = []

    def scheduler_factory(decomposer) -> TaskScheduler:
        strategy = decomposer.strategy
        root = Path(tempfile.mkdtemp(prefix=f"loop-{strategy}-"))
        work_roots.append(root)
        workspace = PythonChangeWorkspace(
            root,
            strategy=strategy,
            read_delay_seconds=read_delay_seconds,
        )
        resolver = InMemoryCapabilityResolver(
            _CAPABILITIES - {"project.inspect.tests"}
        )

        def acquire(capability, node, graph):
            acquired = workspace.acquire(capability)
            if acquired:
                resolver.register(capability)
            return acquired

        def execute(node, graph):
            capability = node.required_capabilities[0]
            if capability == "project.full_change":
                workspace.inspect_source()
                workspace.inspect_tests()
                workspace.apply_change()
                verification = workspace.verify()
                audits.append(
                    workspace.audit(
                        verification_passed=verification["passed"]
                    )
                )
                return _result(
                    verification["passed"],
                    "monolithic project change completed",
                    verification,
                )
            operations = {
                "project.inspect.source": workspace.inspect_source,
                "project.inspect.tests": workspace.inspect_tests,
                "project.apply": workspace.apply_change,
                "project.verify": workspace.verify,
            }
            evidence = operations[capability]()
            passed = evidence.get("passed", True)
            return _result(
                passed,
                f"{capability} completed",
                evidence,
            )

        def integrate(node, graph):
            verification = workspace.verify()
            audits.append(
                workspace.audit(
                    verification_passed=verification["passed"]
                )
            )
            return _result(
                verification["passed"],
                "integrated project change verified",
                verification,
            )

        return TaskScheduler(
            decomposer=decomposer,
            capability_resolver=resolver,
            capability_acquirer=FunctionCapabilityAcquirer(acquire),
            leaf_executor=FunctionLeafExecutor(execute),
            integration_verifier=FunctionIntegrationVerifier(integrate),
            policy=_scheduler_policy(root),
        )

    try:
        comparison = DecompositionStrategyRunner(
            scheduler_factory,
            usage_provider=_BenchmarkUsage(),
            sampling_policy=StrategySamplingPolicy(sample_count=sample_count),
        ).compare(_case(), _strategies())
        ranking = LexicographicStrategyJudge(_judge_policy()).rank(comparison)
        report = ConsolidationBenchmarkReport(
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
        name="python-project-change",
        goal="Add a mean function to a Python calculator and verify the project",
        success_criteria=(
            "source and tests are inspected",
            "the bounded source change is applied",
            "the complete unittest suite passes",
        ),
        required_capabilities=("project.full_change",),
        budget=TaskBudget(max_nodes=8, max_depth=2, max_leaf_executions=6),
    )


def _strategies():
    inspect_source = ChildTaskSpec(
        key="inspect_source",
        goal="Inspect calculator source",
        required_capabilities=["project.inspect.source"],
    )
    inspect_tests = ChildTaskSpec(
        key="inspect_tests",
        goal="Inspect calculator tests",
        required_capabilities=["project.inspect.tests"],
    )
    apply = ChildTaskSpec(
        key="apply",
        goal="Add the bounded mean function",
        required_capabilities=["project.apply"],
        depends_on=["inspect_source", "inspect_tests"],
    )
    verify = ChildTaskSpec(
        key="verify",
        goal="Run the complete unittest suite",
        required_capabilities=["project.verify"],
        depends_on=["apply"],
    )
    sequential_tests = ChildTaskSpec(
        key="inspect_tests",
        goal=inspect_tests.goal,
        required_capabilities=inspect_tests.required_capabilities,
        depends_on=["inspect_source"],
    )
    return {
        "monolithic": lambda: _NamedDecomposer("monolithic", {}),
        "parallel_staged": lambda: _NamedDecomposer(
            "parallel_staged",
            {"root": [inspect_source, inspect_tests, apply, verify]},
        ),
        "sequential_staged": lambda: _NamedDecomposer(
            "sequential_staged",
            {"root": [inspect_source, sequential_tests, apply, verify]},
        ),
    }


def _scheduler_policy(root: Path) -> TaskSchedulerPolicy:
    source = root / "calculator.py"
    tests = root / "test_calculator.py"
    return TaskSchedulerPolicy.create(
        max_parallel_leaves=2,
        parallel_safe_capabilities=_CAPABILITIES,
        mutation_capabilities={"project.full_change", "project.apply"},
        resource_claims={
            "root": [ResourceClaim.workspace(root, mode="write")],
            "root.inspect_source": [
                ResourceClaim.workspace(source, mode="read")
            ],
            "root.inspect_tests": [
                ResourceClaim.workspace(tests, mode="read")
            ],
            "root.apply": [ResourceClaim.workspace(source, mode="write")],
            "root.verify": [ResourceClaim.workspace(root, mode="read")],
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


def _result(passed: bool, summary: str, evidence: dict) -> LeafExecutionResult:
    return LeafExecutionResult(
        status="completed" if passed else "failed",
        summary=summary,
        evidence=evidence,
        error=None if passed else "benchmark_verification_failed",
    )


def _acceptance_checks(
    comparison,
    winners: tuple[str, ...],
    audits: list[BenchmarkAudit],
    sample_count: int,
) -> tuple[BenchmarkAcceptanceCheck, ...]:
    by_strategy = {
        strategy: [audit for audit in audits if audit.strategy == strategy]
        for strategy in _strategies()
    }
    completed = all(run.root_status == "completed" for run in comparison.runs)
    verified = all(audit.verification_passed for audit in audits)
    coverage = all(
        len(strategy_audits) == sample_count
        for strategy_audits in by_strategy.values()
    )
    acquired = all(
        audit.acquired_capabilities == ("project.inspect.tests",)
        for strategy in ("parallel_staged", "sequential_staged")
        for audit in by_strategy[strategy]
    )
    overlap = all(
        audit.independent_reads_overlapped
        for audit in by_strategy["parallel_staged"]
    )
    return (
        BenchmarkAcceptanceCheck(
            "all_strategies_completed",
            completed,
            "every strategy reached completed root status",
        ),
        BenchmarkAcceptanceCheck(
            "isolated_workspaces_verified",
            verified and coverage,
            f"{len(audits)} isolated runs passed the real unittest suite",
        ),
        BenchmarkAcceptanceCheck(
            "capability_acquisition_exercised",
            acquired and coverage,
            "staged strategies acquired project.inspect.tests",
        ),
        BenchmarkAcceptanceCheck(
            "parallel_execution_observed",
            overlap and coverage,
            "parallel strategy overlapped independent source and test reads",
        ),
        BenchmarkAcceptanceCheck(
            "parallel_strategy_ranked_first",
            winners == ("parallel_staged",),
            f"ranking winners: {','.join(winners)}",
        ),
    )
