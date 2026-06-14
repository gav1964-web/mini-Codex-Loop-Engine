from __future__ import annotations

import pytest

from loop_engine.tasks import (
    ChildTaskSpec,
    DecompositionStrategyRunner,
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    LeafExecutionResult,
    ReplayTaskCase,
    ScriptedTaskDecomposer,
    StrategySamplingPolicy,
    StrategyUsage,
    TaskScheduler,
)


def _scheduler(decomposer) -> TaskScheduler:
    return TaskScheduler(
        decomposer=decomposer,
        capability_resolver=InMemoryCapabilityResolver({"work"}),
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: LeafExecutionResult(
                status="completed",
                summary=f"{node.id} completed",
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(),
    )


def _staged() -> ScriptedTaskDecomposer:
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


def test_repeated_latency_samples_use_median_and_mad() -> None:
    comparison = DecompositionStrategyRunner(
        _scheduler,
        sampling_policy=StrategySamplingPolicy(sample_count=3),
        clock=iter([1.0, 1.01, 2.0, 2.1, 3.0, 4.0]).__next__,
    ).compare(
        ReplayTaskCase(name="sampled", goal="Complete work"),
        {"atomic": lambda: ScriptedTaskDecomposer({})},
    )

    run = comparison.runs[0]
    assert run.elapsed_samples_ms == (10, 100, 1000)
    assert run.elapsed_sample_count == 3
    assert run.elapsed_ms == 100
    assert run.elapsed_min_ms == 10
    assert run.elapsed_max_ms == 1000
    assert run.elapsed_mad_ms == 90


def test_repeated_samples_must_preserve_topology() -> None:
    calls = 0

    def changing_strategy():
        nonlocal calls
        calls += 1
        return ScriptedTaskDecomposer({}) if calls == 1 else _staged()

    with pytest.raises(ValueError, match="changed topology"):
        DecompositionStrategyRunner(
            _scheduler,
            sampling_policy=StrategySamplingPolicy(sample_count=3),
            clock=iter([1.0, 1.01, 2.0, 2.01, 3.0, 3.01]).__next__,
        ).compare(
            ReplayTaskCase(name="unstable", goal="Complete work"),
            {"changing": changing_strategy},
        )


def test_usage_provider_runs_once_after_repeated_samples() -> None:
    calls = 0

    class Usage:
        def measure(self, **kwargs):
            nonlocal calls
            calls += 1
            return StrategyUsage(
                input_tokens=1,
                output_tokens=1,
                cost_microunits=1,
                cost_basis="test",
            )

    comparison = DecompositionStrategyRunner(
        _scheduler,
        sampling_policy=StrategySamplingPolicy(sample_count=3),
        usage_provider=Usage(),
        clock=iter([1.0, 1.01, 2.0, 2.01, 3.0, 3.01]).__next__,
    ).compare(
        ReplayTaskCase(name="usage", goal="Complete work"),
        {"atomic": lambda: ScriptedTaskDecomposer({})},
    )

    assert calls == 1
    assert comparison.runs[0].total_tokens == 2


@pytest.mark.parametrize("sample_count", [0, 2, 22, True])
def test_sampling_policy_requires_bounded_odd_count(sample_count) -> None:
    with pytest.raises(ValueError, match="odd and between"):
        StrategySamplingPolicy(sample_count=sample_count)
