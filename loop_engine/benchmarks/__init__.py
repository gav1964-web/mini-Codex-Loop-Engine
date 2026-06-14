"""Reusable end-to-end benchmarks for Loop Engine architecture."""

from .consolidation import run_consolidation_benchmark
from .cross_case import (
    CrossCaseProfileAnalyzer,
    load_benchmark_confidence,
    write_cross_case_profile,
)
from .cross_case_models import (
    CaseRoleResult,
    CrossCaseProfilePolicy,
    CrossCaseProfileReport,
    StrategyRoleProfile,
)
from .history import (
    BenchmarkConfidenceAnalyzer,
    write_benchmark_confidence,
)
from .history_models import (
    BenchmarkConfidencePolicy,
    BenchmarkConfidenceReport,
    BenchmarkHistoryEntry,
    BenchmarkStrategySnapshot,
    StrategyConfidence,
)
from .history_store import JsonBenchmarkHistoryStore
from .models import (
    BenchmarkAcceptanceCheck,
    BenchmarkReport,
    ConsolidationBenchmarkReport,
)
from .project_audit import run_project_audit_benchmark
from .resource_recovery import run_resource_recovery_benchmark
from .retryable_side_effect import run_retryable_side_effect_benchmark

__all__ = [
    "BenchmarkAcceptanceCheck",
    "BenchmarkConfidenceAnalyzer",
    "BenchmarkConfidencePolicy",
    "BenchmarkConfidenceReport",
    "BenchmarkHistoryEntry",
    "BenchmarkReport",
    "BenchmarkStrategySnapshot",
    "CaseRoleResult",
    "ConsolidationBenchmarkReport",
    "CrossCaseProfileAnalyzer",
    "CrossCaseProfilePolicy",
    "CrossCaseProfileReport",
    "JsonBenchmarkHistoryStore",
    "StrategyConfidence",
    "StrategyRoleProfile",
    "load_benchmark_confidence",
    "run_consolidation_benchmark",
    "run_project_audit_benchmark",
    "run_resource_recovery_benchmark",
    "run_retryable_side_effect_benchmark",
    "write_benchmark_confidence",
    "write_cross_case_profile",
]
