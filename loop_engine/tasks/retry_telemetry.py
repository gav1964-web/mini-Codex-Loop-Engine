"""Secret-safe aggregate telemetry derived from retry task events."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .models import TaskGraph

RETRY_TELEMETRY_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RetryTelemetry:
    scheduled: int
    rejected: int
    waits_started: int
    waits_completed: int
    waits_cancelled: int
    total_delay_seconds: float
    total_jitter_seconds: float
    codes: Mapping[str, int]
    rejection_reasons: Mapping[str, int]

    def to_dict(self) -> dict:
        return {
            "schema_version": RETRY_TELEMETRY_SCHEMA_VERSION,
            "scheduled": self.scheduled,
            "rejected": self.rejected,
            "waits_started": self.waits_started,
            "waits_completed": self.waits_completed,
            "waits_cancelled": self.waits_cancelled,
            "total_delay_seconds": self.total_delay_seconds,
            "total_jitter_seconds": self.total_jitter_seconds,
            "codes": dict(self.codes),
            "rejection_reasons": dict(self.rejection_reasons),
        }


def aggregate_retry_telemetry(graph: TaskGraph) -> RetryTelemetry:
    scheduled = rejected = waits_started = waits_completed = 0
    waits_cancelled = 0
    total_delay = total_jitter = 0.0
    codes: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for event in graph.events:
        payload = event.payload
        if event.event_type == "leaf_retry_scheduled":
            scheduled += 1
            total_delay += _finite_non_negative(
                payload.get("delay_seconds")
            )
            total_jitter += _finite_non_negative(
                payload.get("jitter_seconds")
            )
            _increment(codes, payload.get("retry_code"))
        elif event.event_type == "leaf_retry_rejected":
            rejected += 1
            _increment(codes, payload.get("retry_code"))
            _increment(reasons, payload.get("reason"))
        elif event.event_type == "leaf_retry_wait_started":
            waits_started += 1
        elif event.event_type == "leaf_retry_wait_completed":
            waits_completed += 1
            if payload.get("completed") is False:
                waits_cancelled += 1
    return RetryTelemetry(
        scheduled=scheduled,
        rejected=rejected,
        waits_started=waits_started,
        waits_completed=waits_completed,
        waits_cancelled=waits_cancelled,
        total_delay_seconds=round(total_delay, 9),
        total_jitter_seconds=round(total_jitter, 9),
        codes=MappingProxyType(dict(sorted(codes.items()))),
        rejection_reasons=MappingProxyType(dict(sorted(reasons.items()))),
    )


def _increment(target: dict[str, int], value) -> None:
    if isinstance(value, str) and value:
        target[value] = target.get(value, 0) + 1


def _finite_non_negative(value) -> float:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
        and value < float("inf")
    ):
        return float(value)
    return 0.0
