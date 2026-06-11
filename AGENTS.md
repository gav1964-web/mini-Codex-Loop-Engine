# Project Instructions

This project is a universal loop engine, not a coding-agent implementation.

## Boundaries

- `LoopEngine` is the only owner of state transitions.
- Adapters return structured contracts and must not mutate terminal state.
- Domain behavior belongs in adapters or profiles, not in the core engine.
- Keep planner, executor, verifier, judge, and checkpoint ports independent.
- Do not add phrase-specific routing to the core.

## Safety

- Every autonomous run must have explicit budgets.
- Tool failures must become structured `ActionResult` values.
- Long-running subprocess adapters must use the bounded process-tree supervisor.
- Completion requires verifier evidence and a judge decision.
- Repeated observations must trigger stagnation handling.
- Checkpoints must preserve the current loop phase and completed action index.
- Recovery must not repeat actions whose results were durably checkpointed.
- In-flight side effects are at-least-once unless a tool provides idempotency.
- Filesystem tools must resolve every path inside one explicit workspace root.
- File mutations must be bounded, atomic, and idempotent under recovery.
- Do not add unrestricted shell commands or arbitrary filesystem writes.

## Testing

```bash
python -m pytest
python -m compileall -q .
```

Add tests for every new transition or terminal condition.

## Artifacts

Do not commit checkpoints, run logs, environments, caches, secrets, or build
outputs.
