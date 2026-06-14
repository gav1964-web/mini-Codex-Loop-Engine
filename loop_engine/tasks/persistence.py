"""Atomic JSON persistence for task graphs."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import TaskGraph, TaskStatus

TASK_GRAPH_SCHEMA_VERSION = 2
SUPPORTED_TASK_GRAPH_SCHEMA_VERSIONS = frozenset({1, 2})
_GRAPH_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class JsonTaskGraphStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def save(self, graph: TaskGraph) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._target(graph.id)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": TASK_GRAPH_SCHEMA_VERSION,
                    "graph": graph.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        temporary.replace(target)

    def load(self, graph_id: str) -> TaskGraph:
        payload = json.loads(self._target(graph_id).read_text(encoding="utf-8"))
        version = payload.get("schema_version")
        if version not in SUPPORTED_TASK_GRAPH_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported task graph schema version: {version}")
        graph = TaskGraph.from_dict(dict(payload["graph"]))
        for node in graph.nodes.values():
            if node.status == TaskStatus.RUNNING:
                node.status = TaskStatus.READY
                node.error = "recovered_after_interrupted_leaf_execution"
        return graph

    def _target(self, graph_id: str) -> Path:
        if not _GRAPH_ID_PATTERN.fullmatch(graph_id):
            raise ValueError("graph_id may contain only letters, digits, underscore, and hyphen")
        return self.root / f"{graph_id}.json"
