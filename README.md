# mini-Codex Loop Engine

Universal event-driven loop runtime for agentic R&D.

The engine does not assume that a loop is a coding task. It coordinates five
generic concepts:

```text
goal -> plan -> actions -> verification -> judgement -> transition
```

Planners, tools, verifiers, and judges are replaceable adapters. The core owns
state transitions, budgets, stagnation detection, checkpoints, and the event
log.

## Why

mini-Codex 7 proved that planning, tool execution, verification, repair, and
watchdogs are useful, but those responsibilities were still tied to its dialog
shell. Project 8 makes the loop itself the product.

## Quick Start

Python 3.11 or newer:

```bash
python -m pip install -e .[dev]
python -m pytest
python -m loop_engine demo --target 3 --checkpoints checkpoints
```

Run a bounded coding verification command:

```bash
python -m loop_engine check --workspace . --timeout 60 -- python -m pytest
```

Resume an interrupted run:

```bash
python -m loop_engine demo --checkpoints checkpoints --resume RUN_ID
python -m loop_engine check --checkpoints checkpoints --resume RUN_ID
```

Run a bounded scripted repair:

```json
{
  "path": "src/example.py",
  "old_text": "return 1",
  "new_text": "return 2",
  "expected_replacements": 1
}
```

```bash
python -m loop_engine repair \
  --workspace . \
  --patch-file patch.json \
  -- python -m pytest
```

The patch file may also contain an array of repair attempts. A failed
verification triggers the next patch through the normal replan transition.

Run an LLM-planned repair through the gateway from project `5`:

```bash
python -m loop_engine llm-repair \
  --workspace . \
  --goal "Fix the failing test" \
  --gateway-url http://127.0.0.1:8000 \
  --model auto \
  --contract-repair-attempts 1 \
  --checkpoints checkpoints \
  -- python -m pytest
```

The gateway must expose the OpenAI-compatible endpoint
`/v1/chat/completions`. An optional key is read from `LLM_GATEWAY_API_KEY` by
default; only the environment variable name is stored in checkpoints.
Malformed JSON or a schema-invalid plan receives at most one bounded
contract-repair attempt. Disable it with `--contract-repair-attempts 0`.

Run the deterministic Atomic Task Runtime demo:

```bash
python -m loop_engine task-demo --graphs task_graphs
```

It decomposes one parent into dependency-ordered atomic leaves, executes each
leaf through the existing `LoopEngine`, verifies parent integration, and
persists the full graph and event log.

Installed CLI:

```bash
mini-codex-loop demo --target 5
mini-codex-loop check --workspace . -- python -m pytest
```

CLI output is JSON.

## Public API

```python
from loop_engine import LoopBudget, LoopDefinition, LoopEngine

definition = LoopDefinition(
    goal="Reach a verified result",
    success_criteria=["verification passes"],
    budget=LoopBudget(max_iterations=5, max_actions=10),
)

engine = LoopEngine(
    planner=planner,
    executor=executor,
    verifier=verifier,
    judge=judge,
)
state = engine.run(definition)
```

## Current Adapters

- function-backed planner and verifier;
- bounded named-tool registry;
- immutable subprocess specifications;
- process-tree termination on timeout;
- bounded stdout and stderr capture;
- verification-only coding loop profile;
- versioned JSON checkpoints;
- phase-aware recovery without repeating checkpointed actions;
- bounded `list_files`, `read_text`, and `search_text` tools;
- atomic exact-text `apply_patch` with optional SHA-256 precondition;
- deterministic inspect-edit-verify repair profile;
- provider-neutral `JSONLLMClient` port;
- OpenAI-compatible HTTP JSON adapter;
- bounded LLM context builder and strict plan validator;
- one-shot bounded plan contract repair;
- LLM-planned inspect-edit-verify profile;
- persistent `TaskGraph` and iterative dependency scheduler;
- deterministic atomicity/decomposition adapter;
- validated LLM atomicity/decomposition with one-shot contract repair;
- typed atomic leaf contracts for goal, criteria, capabilities, and metadata;
- capability resolver and acquisition port for Plugin Generator integration;
- `LoopEngine`-backed atomic leaf executor;
- coding leaf executor for bounded LLM repair and deterministic verification;
- external immutable workspace and verification policy for coding leaves;
- read-only evidence profile with restricted planning and criterion verification;
- addressable evidence catalogue with strict reference validation;
- bounded subprocess adapter to the standalone Plugin Generator;
- owner/run-aware process registry with heartbeat and stale reaping;
- bounded, interruptible service loop for periodic orphan reaping;
- bounded terminal-record retention and oldest-first registry pruning;
- bounded parallel execution for explicitly admitted independent leaves;
- immutable read/write resource claims for parallel workspace mutation;
- atomic cross-process resource leases for shared scheduler resources;
- context-bound decomposition replay and strategy comparison;
- explicit lexicographic judge policies for decomposition strategy ranking;
- persistent generated-capability registry with artifact integrity checks;
- policy-driven bounded runtime for admitted generated plugins;
- fail-closed WSL bubblewrap sandbox backend for untrusted plugins;
- strict automated release gate for the real sandbox backend;
- bounded parent integration commands and status propagation;
- external routing and all-of composition for parent integration checks;
- typed structural selectors for reusable parent integration routes;
- deterministic criteria judge;
- atomic JSON checkpoint store.

Future adapters can connect LLM planners, subprocess tools, the standalone
mini-Codex Plugin Generator, coding verifiers, and multi-agent workers.

## Status

Version `0.25.0` adds an optional filesystem-backed resource lease manager for
multiple scheduler processes. It atomically acquires every claim in a selected
batch before leaf attempts and execution budget are consumed.

Read leases may coexist; a write lease conflicts with every lease for the same
canonical resource. Contention blocks the affected leaves with structured
evidence, and leases are released after successful, blocked, or failed worker
outcomes. Records abandoned by a dead process are reclaimed only after its PID
identity no longer matches.

Run the shared-registry example with:

```bash
python -m examples.resource_leases_demo
```

The typed integration example remains available as
`python -m examples.integration_composition_demo`.
The strategy comparison and ranking example remains available as
`python -m examples.decomposition_strategy_compare`.
The strict production sandbox gate remains available as
`python -m tools.sandbox_release_gate`.
For explicitly non-production validation, `--degraded-ok` permits an unavailable
backend but reports `degraded`, never `passed`.
The resource-claim example remains available as
`python -m examples.resource_claims_demo`.
The process reaper example remains available as
`python -m examples.process_reaper_service_demo`.
The integration composition example remains available as
`python -m examples.integration_composition_demo`.
The decomposition comparison remains available as
`python -m examples.decomposition_strategy_compare`.
The real sandbox gate remains available as
`python -m examples.plugin_sandbox_smoke`.

See `ARCHITECTURE_RU.md` and `RND_REPORT_RU.md`.
