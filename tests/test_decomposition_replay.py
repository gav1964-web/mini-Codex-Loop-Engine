from __future__ import annotations

import json

import pytest

from loop_engine.tasks import (
    ChildTaskSpec,
    DecompositionStrategyRunner,
    DecompositionTrace,
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    LeafExecutionResult,
    RecordedTaskDecomposer,
    RecordingTaskDecomposer,
    ReplayTaskCase,
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskScheduler,
    TaskStatus,
    StrategyUsage,
)


def _leaf(node, graph) -> LeafExecutionResult:
    return LeafExecutionResult(
        status="completed",
        summary=f"{node.id} completed",
        evidence={"node": node.id},
    )


def _scheduler(decomposer) -> TaskScheduler:
    return TaskScheduler(
        decomposer=decomposer,
        capability_resolver=InMemoryCapabilityResolver({"work"}),
        leaf_executor=FunctionLeafExecutor(_leaf),
        integration_verifier=FunctionIntegrationVerifier(),
    )


def _staged_decomposer() -> ScriptedTaskDecomposer:
    return ScriptedTaskDecomposer(
        {
            "root": [
                ChildTaskSpec(
                    key="inspect",
                    goal="Inspect",
                    required_capabilities=["work"],
                ),
                ChildTaskSpec(
                    key="apply",
                    goal="Apply",
                    required_capabilities=["work"],
                    depends_on=["inspect"],
                ),
            ]
        }
    )


def test_recorded_decomposition_replays_on_fresh_graph(tmp_path) -> None:
    recording = RecordingTaskDecomposer(_staged_decomposer())
    original = _scheduler(recording).run(
        TaskGraph.create("Complete work", graph_id="original")
    )
    trace_path = tmp_path / "trace.json"
    recording.trace().save(trace_path)
    loaded = DecompositionTrace.load(trace_path)
    replay = RecordedTaskDecomposer(loaded)

    reproduced = _scheduler(replay).run(
        TaskGraph.create("Complete work", graph_id="reproduced")
    )

    assert original.root.status == TaskStatus.COMPLETED
    assert reproduced.root.status == TaskStatus.COMPLETED
    assert sorted(original.nodes) == sorted(reproduced.nodes)
    assert original.nodes["root.apply"].dependencies == ["root.inspect"]
    assert replay.unused_node_ids() == []
    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert [row["node_id"] for row in payload["entries"]] == [
        "root",
        "root.inspect",
        "root.apply",
    ]


def test_replay_rejects_changed_node_context() -> None:
    recording = RecordingTaskDecomposer(ScriptedTaskDecomposer({}))
    _scheduler(recording).run(TaskGraph.create("Original goal"))
    replay = RecordedTaskDecomposer(recording.trace())

    result = _scheduler(replay).run(TaskGraph.create("Changed goal"))

    assert result.root.status == TaskStatus.FAILED
    assert result.root.error is not None
    assert "decomposition replay context mismatch: root" in result.root.error


def test_replay_rejects_missing_trace_node() -> None:
    replay = RecordedTaskDecomposer(DecompositionTrace(entries=()))

    result = _scheduler(replay).run(TaskGraph.create("Missing trace"))

    assert result.root.status == TaskStatus.FAILED
    assert "decomposition trace has no node: root" in result.root.error


def test_trace_loader_rejects_duplicate_node_ids(tmp_path) -> None:
    path = tmp_path / "trace.json"
    row = {
        "node_id": "root",
        "context_sha256": "0" * 64,
        "decision": {"is_atomic": True, "reason": "x", "children": [], "leaf": None},
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [row, row],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="node ids must be unique"):
        DecompositionTrace.load(path)


def test_strategy_runner_detects_topology_and_outcome_divergence() -> None:
    runner = DecompositionStrategyRunner(_scheduler)
    case = ReplayTaskCase(
        name="work-case",
        goal="Complete work",
        required_capabilities=("work",),
    )

    comparison = runner.compare(
        case,
        {
            "atomic": lambda: ScriptedTaskDecomposer({}),
            "staged": _staged_decomposer,
        },
    )

    assert [run.strategy for run in comparison.runs] == ["atomic", "staged"]
    assert comparison.topology_diverged is True
    assert comparison.outcome_diverged is True
    atomic, staged = comparison.runs
    assert atomic.node_count == 1
    assert atomic.leaf_count == 1
    assert staged.node_count == 3
    assert staged.leaf_count == 2
    assert staged.dependency_edge_count == 1
    assert staged.leaf_executions == 2


def test_strategy_runner_records_elapsed_and_external_usage() -> None:
    ticks = iter([10.0, 10.125, 20.0, 20.25])

    class Usage:
        def measure(self, *, strategy, case, graph):
            multiplier = 1 if strategy == "atomic" else 2
            return StrategyUsage(
                input_tokens=100 * multiplier,
                output_tokens=25 * multiplier,
                cost_microunits=50 * multiplier,
                cost_basis="test-units-v1",
            )

    comparison = DecompositionStrategyRunner(
        _scheduler,
        usage_provider=Usage(),
        clock=lambda: next(ticks),
    ).compare(
        ReplayTaskCase(name="measured", goal="Complete work"),
        {
            "atomic": lambda: ScriptedTaskDecomposer({}),
            "staged": _staged_decomposer,
        },
    )

    atomic, staged = comparison.runs
    assert atomic.elapsed_ms == 125
    assert staged.elapsed_ms == 250
    assert atomic.total_tokens == 125
    assert staged.total_tokens == 250
    assert atomic.cost_microunits == 50
    assert atomic.cost_basis == "test-units-v1"


def test_strategy_runner_leaves_usage_unmeasured_without_provider() -> None:
    comparison = DecompositionStrategyRunner(
        _scheduler,
        clock=iter([1.0, 1.01]).__next__,
    ).compare(
        ReplayTaskCase(name="unmeasured", goal="Complete work"),
        {"atomic": lambda: ScriptedTaskDecomposer({})},
    )

    run = comparison.runs[0]
    assert run.elapsed_ms == 10
    assert run.input_tokens is None
    assert run.cost_microunits is None
    assert run.cost_basis is None


def test_strategy_runner_rejects_clock_moving_backwards() -> None:
    with pytest.raises(ValueError, match="clock moved backwards"):
        DecompositionStrategyRunner(
            _scheduler,
            clock=iter([2.0, 1.0]).__next__,
        ).compare(
            ReplayTaskCase(name="clock", goal="Complete work"),
            {"atomic": lambda: ScriptedTaskDecomposer({})},
        )


def test_usage_contract_rejects_invalid_values_and_provider_type() -> None:
    with pytest.raises(ValueError, match="non-negative integers"):
        StrategyUsage(
            input_tokens=-1,
            output_tokens=0,
            cost_microunits=0,
            cost_basis="x",
        )
    with pytest.raises(ValueError, match="cost_basis"):
        StrategyUsage(
            input_tokens=0,
            output_tokens=0,
            cost_microunits=0,
            cost_basis=" ",
        )

    class InvalidUsage:
        def measure(self, **kwargs):
            return {"input_tokens": 1}

    with pytest.raises(TypeError, match="StrategyUsage"):
        DecompositionStrategyRunner(
            _scheduler,
            usage_provider=InvalidUsage(),
            clock=iter([1.0, 2.0]).__next__,
        ).compare(
            ReplayTaskCase(name="invalid-usage", goal="Complete work"),
            {"atomic": lambda: ScriptedTaskDecomposer({})},
        )


def test_usage_provider_cannot_rewrite_captured_strategy_metrics() -> None:
    class MutatingUsage:
        def measure(self, *, strategy, case, graph):
            graph.root.status = TaskStatus.FAILED
            graph.nodes.clear()
            return StrategyUsage(
                input_tokens=1,
                output_tokens=1,
                cost_microunits=1,
                cost_basis="test",
            )

    comparison = DecompositionStrategyRunner(
        _scheduler,
        usage_provider=MutatingUsage(),
        clock=iter([1.0, 1.01]).__next__,
    ).compare(
        ReplayTaskCase(name="provider-isolation", goal="Complete work"),
        {"atomic": lambda: ScriptedTaskDecomposer({})},
    )

    run = comparison.runs[0]
    assert run.root_status == "completed"
    assert run.node_count == 1
    assert run.total_tokens == 2


def test_strategy_comparison_saves_versioned_json(tmp_path) -> None:
    comparison = DecompositionStrategyRunner(_scheduler).compare(
        ReplayTaskCase(name="report", goal="Complete work"),
        {"atomic": lambda: ScriptedTaskDecomposer({})},
    )
    path = tmp_path / "comparison.json"

    comparison.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["case"] == "report"
    assert payload["topology_diverged"] is False
    assert payload["runs"][0]["strategy"] == "atomic"


def test_equivalent_strategies_share_stable_fingerprints() -> None:
    runner = DecompositionStrategyRunner(_scheduler)
    case = ReplayTaskCase(name="stable", goal="Complete work")

    first = runner.compare(
        case,
        {
            "one": _staged_decomposer,
            "two": _staged_decomposer,
        },
    )
    second = runner.compare(
        case,
        {
            "one": _staged_decomposer,
            "two": _staged_decomposer,
        },
    )

    assert first.topology_diverged is False
    assert first.outcome_diverged is False
    assert first.runs[0].topology_sha256 == second.runs[0].topology_sha256
    assert first.runs[0].outcome_sha256 == second.runs[0].outcome_sha256
    assert len(first.topology_groups) == 1
    assert next(iter(first.topology_groups.values())) == ("one", "two")


def test_strategy_runner_requires_at_least_one_strategy() -> None:
    runner = DecompositionStrategyRunner(_scheduler)

    with pytest.raises(ValueError, match="at least one"):
        runner.compare(ReplayTaskCase(name="empty", goal="Work"), {})


def test_trace_loader_rejects_invalid_context_hash(tmp_path) -> None:
    path = tmp_path / "trace.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "node_id": "root",
                        "context_sha256": "not-a-hash",
                        "decision": {
                            "is_atomic": True,
                            "reason": "atomic",
                            "children": [],
                            "leaf": None,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="identity fields"):
        DecompositionTrace.load(path)
