"""Parent/dependency propagation methods mixed into TaskScheduler."""

from __future__ import annotations

from .events import record_task_event
from .models import LeafExecutionResult, TaskGraph, TaskStatus


class TaskSchedulerPropagation:
    def _propagate(self, graph: TaskGraph) -> bool:
        changed = False
        for node in sorted(
            graph.nodes.values(),
            key=lambda item: item.depth,
            reverse=True,
        ):
            if node.status in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.BLOCKED,
            }:
                continue
            dependency_states = [
                graph.nodes[item].status for item in node.dependencies
            ]
            if any(
                status in {TaskStatus.FAILED, TaskStatus.BLOCKED}
                for status in dependency_states
            ):
                node.status = TaskStatus.BLOCKED
                node.error = "dependency_failed_or_blocked"
                record_task_event(
                    graph,
                    "task_blocked",
                    node.id,
                    {"reason": node.error},
                )
                self._save(graph)
                changed = True
                continue
            if node.children:
                changed = self._propagate_parent(graph, node) or changed
        return changed

    def _propagate_parent(self, graph, node) -> bool:
        child_states = [graph.nodes[item].status for item in node.children]
        if any(
            status in {TaskStatus.FAILED, TaskStatus.BLOCKED}
            for status in child_states
        ):
            node.status = TaskStatus.BLOCKED
            node.error = "child_failed_or_blocked"
            record_task_event(
                graph,
                "task_blocked",
                node.id,
                {"reason": node.error},
            )
            self._save(graph)
            return True
        if not all(status == TaskStatus.COMPLETED for status in child_states):
            return False
        try:
            result = self.integration_verifier.verify(node, graph)
        except Exception as exc:
            result = LeafExecutionResult(
                status="failed",
                summary="integration verifier raised an exception",
                error=f"{type(exc).__name__}: {exc}",
            )
        node.result = result
        if result.status == "completed":
            node.status = TaskStatus.COMPLETED
            event_type = "integration_completed"
            payload = {"summary": result.summary}
        else:
            node.status = (
                TaskStatus.BLOCKED
                if result.status == "blocked"
                else TaskStatus.FAILED
            )
            node.error = result.error or result.summary
            event_type = "integration_failed"
            payload = {"error": node.error}
        record_task_event(graph, event_type, node.id, payload)
        self._save(graph)
        return True
