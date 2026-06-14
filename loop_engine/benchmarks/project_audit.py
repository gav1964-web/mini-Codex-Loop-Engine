"""Read-only multi-source project audit benchmark."""

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
from .audit_workspace import ProjectAudit, ProjectAuditWorkspace
from .models import BenchmarkAcceptanceCheck, ConsolidationBenchmarkReport

_CAPABILITIES = {
    "project.audit.full",
    "project.audit.source",
    "project.audit.docs",
    "project.audit.config",
}


class _NamedDecomposer(ScriptedTaskDecomposer):
    def __init__(self, strategy: str, decompositions) -> None:
        super().__init__(decompositions)
        self.strategy = strategy


class _AuditUsage:
    def measure(self, *, strategy, case, graph) -> StrategyUsage:
        estimates = {
            "monolithic": (720, 150, 720),
            "parallel_evidence": (480, 100, 480),
            "sequential_evidence": (480, 100, 480),
        }
        input_tokens, output_tokens, cost = estimates[strategy]
        return StrategyUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microunits=cost,
            cost_basis="benchmark-estimate-v1",
        )


def run_project_audit_benchmark(
    output_path: str | Path = "build/project_audit_benchmark/report.json",
    *,
    sample_count: int = 3,
    read_delay_seconds: float = 0.04,
) -> ConsolidationBenchmarkReport:
    audits: list[ProjectAudit] = []
    work_roots: list[Path] = []

    def scheduler_factory(decomposer) -> TaskScheduler:
        strategy = decomposer.strategy
        root = Path(tempfile.mkdtemp(prefix=f"audit-{strategy}-"))
        work_roots.append(root)
        workspace = ProjectAuditWorkspace(
            root,
            strategy=strategy,
            read_delay_seconds=read_delay_seconds,
        )
        resolver = InMemoryCapabilityResolver(
            _CAPABILITIES - {"project.audit.docs"}
        )

        def acquire(capability, node, graph):
            acquired = workspace.acquire(capability)
            if acquired:
                resolver.register(capability)
            return acquired

        def execute(node, graph):
            capability = node.required_capabilities[0]
            if capability == "project.audit.full":
                evidence = workspace.verify()
                audits.append(workspace.audit(verified=evidence["passed"]))
                return _result(
                    evidence["passed"],
                    "monolithic project audit completed",
                    evidence,
                )
            operations = {
                "project.audit.source": workspace.inspect_source,
                "project.audit.docs": workspace.inspect_docs,
                "project.audit.config": workspace.inspect_config,
            }
            return _result(
                True,
                f"{capability} completed",
                operations[capability](),
            )

        def integrate(node, graph):
            children = [
                graph.nodes[child_id].result.evidence
                for child_id in node.children
                if graph.nodes[child_id].result is not None
            ]
            combined = {
                key: value
                for evidence in children
                for key, value in evidence.items()
            }
            passed = all(
                (
                    combined.get("has_normalize"),
                    combined.get("documents_check_command"),
                    combined.get("requires_python_311"),
                )
            )
            audits.append(workspace.audit(verified=passed))
            return _result(
                passed,
                "integrated project audit verified",
                {"passed": passed, **combined},
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
            usage_provider=_AuditUsage(),
            sampling_policy=StrategySamplingPolicy(sample_count=sample_count),
        ).compare(_case(), _strategies())
        ranking = LexicographicStrategyJudge(_judge_policy()).rank(comparison)
        report = ConsolidationBenchmarkReport(
            benchmark="python-project-audit",
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
        name="python-project-audit",
        goal="Audit source, documentation, and configuration evidence",
        success_criteria=(
            "source behavior is identified",
            "verification command is documented",
            "Python version policy is identified",
        ),
        required_capabilities=("project.audit.full",),
        budget=TaskBudget(max_nodes=6, max_depth=2, max_leaf_executions=4),
    )


def _strategies():
    source = ChildTaskSpec(
        key="source",
        goal="Inspect source behavior",
        required_capabilities=["project.audit.source"],
    )
    docs = ChildTaskSpec(
        key="docs",
        goal="Inspect documented verification",
        required_capabilities=["project.audit.docs"],
    )
    config = ChildTaskSpec(
        key="config",
        goal="Inspect Python version policy",
        required_capabilities=["project.audit.config"],
    )
    sequential_docs = ChildTaskSpec(
        key="docs",
        goal=docs.goal,
        required_capabilities=docs.required_capabilities,
        depends_on=["source"],
    )
    sequential_config = ChildTaskSpec(
        key="config",
        goal=config.goal,
        required_capabilities=config.required_capabilities,
        depends_on=["docs"],
    )
    return {
        "monolithic": lambda: _NamedDecomposer("monolithic", {}),
        "parallel_evidence": lambda: _NamedDecomposer(
            "parallel_evidence",
            {"root": [source, docs, config]},
        ),
        "sequential_evidence": lambda: _NamedDecomposer(
            "sequential_evidence",
            {"root": [source, sequential_docs, sequential_config]},
        ),
    }


def _scheduler_policy(root: Path) -> TaskSchedulerPolicy:
    return TaskSchedulerPolicy.create(
        max_parallel_leaves=3,
        parallel_safe_capabilities=_CAPABILITIES,
        resource_claims={
            "root": [ResourceClaim.workspace(root, mode="read")],
            "root.source": [
                ResourceClaim.workspace(root / "app.py", mode="read")
            ],
            "root.docs": [
                ResourceClaim.workspace(root / "README.md", mode="read")
            ],
            "root.config": [
                ResourceClaim.workspace(root / "pyproject.toml", mode="read")
            ],
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
        error=None if passed else "project_audit_verification_failed",
    )


def _acceptance_checks(
    comparison,
    winners: tuple[str, ...],
    audits: list[ProjectAudit],
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
    acquired = all(
        audit.acquired_capabilities == ("project.audit.docs",)
        for strategy in ("parallel_evidence", "sequential_evidence")
        for audit in by_strategy[strategy]
    )
    overlap = all(
        audit.independent_reads_overlapped
        for audit in by_strategy["parallel_evidence"]
    )
    return (
        BenchmarkAcceptanceCheck(
            "all_strategies_completed",
            all(run.root_status == "completed" for run in comparison.runs),
            "every audit strategy reached completed root status",
        ),
        BenchmarkAcceptanceCheck(
            "read_only_evidence_verified",
            coverage and all(audit.verified for audit in audits),
            f"{len(audits)} isolated project audits verified expected evidence",
        ),
        BenchmarkAcceptanceCheck(
            "capability_acquisition_exercised",
            coverage and acquired,
            "evidence strategies acquired project.audit.docs",
        ),
        BenchmarkAcceptanceCheck(
            "parallel_execution_observed",
            coverage and overlap,
            "parallel strategy overlapped three independent reads",
        ),
        BenchmarkAcceptanceCheck(
            "parallel_strategy_ranked_first",
            winners == ("parallel_evidence",),
            f"ranking winners: {','.join(winners)}",
        ),
    )
