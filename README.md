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
- deterministic criteria judge;
- atomic JSON checkpoint store.

Future adapters can connect LLM planners, subprocess tools, the standalone
mini-Codex Plugin Generator, coding verifiers, and multi-agent workers.

## Status

Version `0.3.0` is a deterministic MVP with bounded subprocess execution,
phase-aware checkpoint recovery, and a first coding verification profile. It
deliberately excludes LLM provider code, filesystem editing, autonomous repair,
and distributed workers until their contracts can be added without weakening
the loop kernel.

See `ARCHITECTURE_RU.md` and `RND_REPORT_RU.md`.
