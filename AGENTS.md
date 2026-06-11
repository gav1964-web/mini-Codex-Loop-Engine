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
- LLM output is an untrusted proposal and must pass deterministic schema validation.
- LLM planners may choose actions but may not execute tools or decide completion.
- Provider credentials must stay in environment variables and out of checkpoints.
- Contract repair may retry malformed LLM structure at most once.
- Contract repair must not execute tools, alter goals, or reinterpret verification.
- Transport failures are not contract errors and must not trigger repair prompts.
- `TaskScheduler` is the only owner of task-node status transitions.
- Task decomposition, capability resolution/acquisition, leaf execution, and
  parent integration must remain independent ports.
- A non-atomic node must not execute directly; it must produce bounded children.
- LLM decomposition must pass strict schema, dependency, cycle, and leaf-contract
  validation before the scheduler mutates a graph.
- An LLM-declared atomic leaf must have observable success criteria and explicit
  required capabilities.
- Coding leaf workspace, verification commands, gateway configuration, and
  credentials must come from external policy, never from task metadata.
- Read-only coding leaves may complete only through the strict evidence
  verifier: every criterion must appear exactly once and cite existing bounded
  evidence catalogue ids.
- Parent completion requires completed children and an integration verifier result.
- Plugin generation belongs behind `CapabilityAcquirer`, not inside the scheduler.

## Testing

```bash
python -m pytest
python -m compileall -q .
```

Add tests for every new transition or terminal condition.

## Artifacts

Do not commit checkpoints, run logs, environments, caches, secrets, or build
outputs.
