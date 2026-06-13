"""Thread-safe lifecycle registry for bounded child processes."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    record_id: str
    owner_run_id: str
    pid: int
    process_identity: str
    command_sha256: str
    cwd: str
    timeout_seconds: float
    hostname: str
    status: str
    started_at: float
    heartbeat_at: float
    finished_at: float | None = None
    exit_code: int | None = None
    reason: str | None = None


class ProcessRegistry:
    """Track process ownership, heartbeats, and terminal outcomes."""

    def __init__(
        self,
        storage_path: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.storage_path = (
            Path(storage_path).resolve() if storage_path is not None else None
        )
        self.clock = clock
        self._lock = threading.RLock()
        self._records: dict[str, ProcessRecord] = {}
        self._load()

    def register(
        self,
        *,
        owner_run_id: str,
        pid: int,
        process_identity: str,
        argv: tuple[str, ...],
        cwd: str,
        timeout_seconds: float,
    ) -> ProcessRecord:
        owner = owner_run_id.strip()
        if not owner:
            raise ValueError("process owner_run_id is required")
        if pid <= 0 or not process_identity:
            raise ValueError("process pid and identity are required")
        now = self.clock()
        record = ProcessRecord(
            record_id=uuid4().hex,
            owner_run_id=owner,
            pid=pid,
            process_identity=process_identity,
            command_sha256=_command_digest(argv),
            cwd=str(cwd),
            timeout_seconds=timeout_seconds,
            hostname=socket.gethostname(),
            status="running",
            started_at=now,
            heartbeat_at=now,
        )
        with self._lock:
            self._records[record.record_id] = record
            self._save()
        return record

    def heartbeat(self, record_id: str) -> ProcessRecord:
        with self._lock:
            current = self._require_running(record_id)
            updated = _replace_record(current, heartbeat_at=self.clock())
            self._records[record_id] = updated
            self._save()
            return updated

    def finish(
        self,
        record_id: str,
        *,
        status: str,
        exit_code: int | None,
        reason: str | None = None,
    ) -> ProcessRecord:
        if status == "running":
            raise ValueError("terminal process status is required")
        with self._lock:
            current = self._records.get(record_id)
            if current is None:
                raise KeyError(f"unknown process record: {record_id}")
            if current.status != "running":
                return current
            now = self.clock()
            updated = _replace_record(
                current,
                status=status,
                heartbeat_at=now,
                finished_at=now,
                exit_code=exit_code,
                reason=reason,
            )
            self._records[record_id] = updated
            self._save()
            return updated

    def get(self, record_id: str) -> ProcessRecord | None:
        with self._lock:
            return self._records.get(record_id)

    def records(self, *, status: str | None = None) -> list[ProcessRecord]:
        with self._lock:
            values = list(self._records.values())
        if status is not None:
            values = [record for record in values if record.status == status]
        return sorted(values, key=lambda record: (record.started_at, record.record_id))

    def prune_terminal(
        self,
        *,
        retain_seconds: float,
        max_records: int | None = None,
    ) -> int:
        if retain_seconds < 0:
            raise ValueError("retain_seconds must not be negative")
        if max_records is not None and (
            not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or max_records <= 0
        ):
            raise ValueError("max_records must be positive")
        cutoff = self.clock() - retain_seconds
        with self._lock:
            removable = sorted(
                (
                    record
                    for record in self._records.values()
                    if record.status != "running"
                    and record.finished_at is not None
                    and record.finished_at < cutoff
                ),
                key=lambda record: (record.finished_at, record.record_id),
            )
            if max_records is not None:
                removable = removable[:max_records]
            for record in removable:
                del self._records[record.record_id]
            if removable:
                self._save()
            return len(removable)

    def reap_stale(
        self,
        *,
        stale_after_seconds: float,
        identity_lookup: Callable[[int], str | None],
        terminate: Callable[[int], None],
    ) -> list[ProcessRecord]:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        now = self.clock()
        candidates = [
            record
            for record in self.records(status="running")
            if now - record.heartbeat_at > stale_after_seconds
        ]
        reaped: list[ProcessRecord] = []
        for record in candidates:
            identity = identity_lookup(record.pid)
            if identity != record.process_identity:
                reaped.append(
                    self.finish(
                        record.record_id,
                        status="lost",
                        exit_code=None,
                        reason="process_identity_missing_or_changed",
                    )
                )
                continue
            terminate(record.pid)
            reaped.append(
                self.finish(
                    record.record_id,
                    status="terminated",
                    exit_code=None,
                    reason="stale_heartbeat",
                )
            )
        return reaped

    def _require_running(self, record_id: str) -> ProcessRecord:
        current = self._records.get(record_id)
        if current is None:
            raise KeyError(f"unknown process record: {record_id}")
        if current.status != "running":
            raise ValueError(f"process record is already terminal: {record_id}")
        return current

    def _load(self) -> None:
        if self.storage_path is None or not self.storage_path.exists():
            return
        payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported process registry schema_version")
        rows = payload.get("records")
        if not isinstance(rows, list):
            raise ValueError("process registry records must be an array")
        loaded: dict[str, ProcessRecord] = {}
        for row in rows:
            record = ProcessRecord(**dict(row))
            if record.record_id in loaded:
                raise ValueError("duplicate process registry record_id")
            loaded[record.record_id] = record
        self._records = loaded

    def _save(self) -> None:
        if self.storage_path is None:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "records": [
                asdict(self._records[key]) for key in sorted(self._records)
            ],
        }
        temporary = self.storage_path.with_name(f".{self.storage_path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.storage_path)


def _command_digest(argv: tuple[str, ...]) -> str:
    encoded = json.dumps(list(argv), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _replace_record(record: ProcessRecord, **changes) -> ProcessRecord:
    values = asdict(record)
    values.update(changes)
    return ProcessRecord(**values)


_GLOBAL_PROCESS_REGISTRY = ProcessRegistry()


def get_global_process_registry() -> ProcessRegistry:
    return _GLOBAL_PROCESS_REGISTRY


def configure_global_process_registry(
    storage_path: str | Path | None,
) -> ProcessRegistry:
    global _GLOBAL_PROCESS_REGISTRY
    _GLOBAL_PROCESS_REGISTRY = ProcessRegistry(storage_path)
    return _GLOBAL_PROCESS_REGISTRY
