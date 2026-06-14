"""Benchmark resource contention and recovery from interrupted leaves."""

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
    JsonTaskGraphStore,
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
from .models import BenchmarkAcceptanceCheck, BenchmarkReport
from .recovery_workspace import RecoveryAudit, RecoveryWorkspace

_CAPABILITIES = {
    "recovery.full",
    "recovery.inspect",
    "recovery.write_a",
    "recovery.write_b",
}


class _NamedDecomposer(ScriptedTaskDecomposer):
    def __init__(self, strategy: str, decompositions) -> None:
        super().__init__(decompositions)
        self.strategy = strategy


class _RecoveryUsage:
    def measure(self, *, strategy, case, graph) -> StrategyUsage:
        estimates = {
            "monolithic": (900, 180, 900),
            "parallel_recovery": (650, 130, 650),
            "sequential_recovery": (650, 130, 650),
        }
        input_tokens, output_tokens, cost = estimates[strategy]
        return StrategyUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microunits=cost,
            cost_basis="benchmark-estimate-v1",
        )


class _RecoveringScheduler:
    def __init__(
        self,
        *,
        build_scheduler,
        store: JsonTaskGraphStore,
        graph_id: str,
    ) -> None:
        self.build_scheduler = build_scheduler
        self.store = store
        self.graph_id = graph_id

    def run(self, graph):
        try:
            self.build_scheduler().run(graph)
        except SystemExit as exc:
            if str(exc) != "simulated_process_interruption":
                raise
        recovered = self.store.load(self.graph_id)
        return self.build_scheduler().run(recovered)


def run_resource_recovery_benchmark(
    output_path: str | Path = "build/resource_recovery_benchmark/report.json",
    *,
    sample_count: int = 3,
    operation_delay_seconds: float = 0.04,
) -> BenchmarkReport:
    audits: list[RecoveryAudit] = []
    work_roots: list[Path] = []

    def scheduler_factory(decomposer):
        strategy = decomposer.strategy
        root = Path(tempfile.mkdtemp(prefix=f"recovery-{strategy}-"))
        work_roots.append(root)
        workspace = RecoveryWorkspace(
            root / "workspace",
            strategy=strategy,
            operation_delay_seconds=operation_delay_seconds,
        )
        store = JsonTaskGraphStore(root / "graphs")

        def execute(node, graph):
            capability = node.required_capabilities[0]
            operations = {
                "recovery.full": workspace.full_with_interruption,
                "recovery.inspect": workspace.inspect,
                "recovery.write_a": workspace.write_a,
                "recovery.write_b": workspace.write_b_with_interruption,
            }
            evidence = operations[capability]()
            if capability == "recovery.full":
                audits.append(
                    workspace.audit(graph, verified=evidence["passed"])
                )
            return LeafExecutionResult(
                status="completed",
                summary=f"{capability} completed",
                evidence=evidence,
            )

        def integrate(node, graph):
            verification = workspace.verify()
            audits.append(
                workspace.audit(
                    graph,
                    verified=verification["passed"],
                )
            )
            return _result(verification)

        def build_scheduler():
            return TaskScheduler(
                decomposer=decomposer,
                capability_resolver=InMemoryCapabilityResolver(_CAPABILITIES),
                leaf_executor=FunctionLeafExecutor(execute),
                integration_verifier=FunctionIntegrationVerifier(integrate),
                store=store,
                policy=_scheduler_policy(root / "workspace"),
            )

        return _RecoveringScheduler(
            build_scheduler=build_scheduler,
            store=store,
            graph_id=_stable_graph_id("resource-contention-recovery", strategy),
        )

    try:
        comparison = DecompositionStrategyRunner(
            scheduler_factory,
            usage_provider=_RecoveryUsage(),
            sampling_policy=StrategySamplingPolicy(sample_count=sample_count),
        ).compare(_case(), _strategies())
        ranking = LexicographicStrategyJudge(_judge_policy()).rank(comparison)
        report = BenchmarkReport(
            benchmark="resource-contention-recovery",
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
        name="resource-contention-recovery",
        goal="Apply two conflicting writes and recover an interrupted task",
        success_criteria=(
            "independent work may overlap",
            "conflicting writes do not overlap",
            "interrupted work resumes without repeating completed leaves",
        ),
        required_capabilities=("recovery.full",),
        budget=TaskBudget(max_nodes=6, max_depth=2, max_leaf_executions=6),
    )


def _strategies():
    inspect = ChildTaskSpec(
        key="inspect",
        goal="Inspect initial state",
        required_capabilities=["recovery.inspect"],
    )
    write_a = ChildTaskSpec(
        key="write_a",
        goal="Write A",
        required_capabilities=["recovery.write_a"],
    )
    write_b = ChildTaskSpec(
        key="write_b",
        goal="Write B after interruption recovery",
        required_capabilities=["recovery.write_b"],
        depends_on=["write_a"],
    )
    sequential_a = ChildTaskSpec(
        key="write_a",
        goal=write_a.goal,
        required_capabilities=write_a.required_capabilities,
        depends_on=["inspect"],
    )
    return {
        "monolithic": lambda: _NamedDecomposer("monolithic", {}),
        "parallel_recovery": lambda: _NamedDecomposer(
            "parallel_recovery",
            {"root": [inspect, write_a, write_b]},
        ),
        "sequential_recovery": lambda: _NamedDecomposer(
            "sequential_recovery",
            {"root": [inspect, sequential_a, write_b]},
        ),
    }


def _scheduler_policy(root: Path) -> TaskSchedulerPolicy:
    shared = root / "state.txt"
    return TaskSchedulerPolicy.create(
        max_parallel_leaves=2,
        parallel_safe_capabilities=_CAPABILITIES,
        mutation_capabilities={
            "recovery.full",
            "recovery.write_a",
            "recovery.write_b",
        },
        resource_claims={
            "root": [ResourceClaim.workspace(shared, mode="write")],
            "root.inspect": [
                ResourceClaim.create("evidence:initial", mode="read")
            ],
            "root.write_a": [
                ResourceClaim.workspace(shared, mode="write")
            ],
            "root.write_b": [
                ResourceClaim.workspace(shared, mode="write")
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


def _result(verification: dict) -> LeafExecutionResult:
    passed = verification["passed"]
    return LeafExecutionResult(
        status="completed" if passed else "failed",
        summary="recovered resource writes verified",
        evidence=verification,
        error=None if passed else "resource_recovery_verification_failed",
    )


def _acceptance_checks(
    comparison,
    winners: tuple[str, ...],
    audits: list[RecoveryAudit],
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
    staged = [
        audit
        for strategy in ("parallel_recovery", "sequential_recovery")
        for audit in by_strategy[strategy]
    ]
    return (
        BenchmarkAcceptanceCheck(
            "all_strategies_recovered",
            coverage
            and all(run.root_status == "completed" for run in comparison.runs)
            and all(audit.verified for audit in audits),
            "every interrupted strategy resumed and verified AB state",
        ),
        BenchmarkAcceptanceCheck(
            "interruption_recorded_once",
            coverage
            and all(audit.interruption_count == 1 for audit in audits),
            "each isolated run simulated exactly one process interruption",
        ),
        BenchmarkAcceptanceCheck(
            "recovery_marker_observed",
            coverage
            and all(audit.recovery_markers == 1 for audit in audits),
            "each recovered graph retained one running-to-ready marker",
        ),
        BenchmarkAcceptanceCheck(
            "completed_leaves_not_reexecuted",
            coverage
            and all(
                audit.completed_leaf_reexecutions == 0 for audit in staged
            ),
            "completed staged leaves were not executed after recovery",
        ),
        BenchmarkAcceptanceCheck(
            "resource_conflict_serialized",
            coverage
            and all(not audit.conflicting_write_overlap for audit in staged),
            "write_a and write_b never overlapped on the shared resource",
        ),
        BenchmarkAcceptanceCheck(
            "independent_work_overlapped",
            coverage
            and all(
                audit.independent_overlap
                for audit in by_strategy["parallel_recovery"]
            ),
            "parallel recovery overlapped inspect with write_a",
        ),
        BenchmarkAcceptanceCheck(
            "parallel_strategy_ranked_first",
            winners == ("parallel_recovery",),
            f"ranking winners: {','.join(winners)}",
        ),
    )


def _stable_graph_id(case: str, strategy: str) -> str:
    import hashlib

    digest = hashlib.sha256(f"{case}\0{strategy}".encode()).hexdigest()
    return f"replay-{digest[:16]}"
