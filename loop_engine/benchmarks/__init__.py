"""Reusable end-to-end benchmarks for Loop Engine architecture."""

from .consolidation import run_consolidation_benchmark
from .models import (
    BenchmarkAcceptanceCheck,
    ConsolidationBenchmarkReport,
)

__all__ = [
    "BenchmarkAcceptanceCheck",
    "ConsolidationBenchmarkReport",
    "run_consolidation_benchmark",
]
