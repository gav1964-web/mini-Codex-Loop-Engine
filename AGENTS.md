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
- Every bounded subprocess must remain registered with owner/run id and heartbeat
  until it reaches a terminal process outcome.
- Periodic orphan reaping must run through an explicitly owned bounded service
  loop with a cycle budget and interruptible stop signal; no hidden daemon.
- Only one reaper service run may own a registry adapter instance at a time.
- Terminal process retention must be opt-in, cadence-bounded, count-bounded, and
  oldest-first. Running records must never be pruned.
- A pruning failure must retain completed reaping evidence and fail the service
  report explicitly.
- Bounded services may emit observability only through an explicit typed report
  sink. Configured report persistence failures must fail the run visibly.
- Service-run reports must use versioned atomic persistence, bounded listing,
  path-safe identifiers, and must not contain raw process commands or secrets.
- Persistent process registries must store command digests, not raw argv that may
  contain credentials.
- Completion requires verifier evidence and a judge decision.
- Repeated observations must trigger stagnation handling.
- Checkpoints must preserve the current loop phase and completed action index.
- Recovery must not repeat actions whose results were durably checkpointed.
- In-flight side effects are at-least-once unless a tool provides idempotency.
- Write-resource leases must carry persistent monotonic fencing tokens. A
  dangerous adapter may claim fencing safety only when it atomically rejects
  stale tokens at the side-effect boundary.
- Fencing counters must survive release and expiry; schema downgrade or missing
  counters must fail closed rather than reset token history.
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
- Parallel leaf execution must be explicitly bounded and limited to capabilities
  admitted as parallel-safe by external scheduler policy.
- Parallel mutation requires immutable external resource claims. A mutation leaf
  without at least one write claim must remain sequential.
- Resource claims must use canonical resource identities; task metadata must not
  grant, remove, or rewrite claims.
- Multiple scheduler processes that can touch the same claimed resources must
  share an explicit cross-process lease backend.
- Resource leases must acquire the full batch atomically before leaf attempts
  or execution budget are consumed, and must be released after every outcome.
- Active resource leases must be renewed by an explicitly owned heartbeat with
  a bounded interval shorter than the lease TTL.
- Expired leases may be reclaimed even while the owner process is alive; late
  renewal must never resurrect an expired lease.
- Heartbeat setup or renewal failure must fail the leased task outcome and stop
  the heartbeat before release.
- Lease contention and registry failures must become structured task outcomes;
  workers must never own lease acquisition or task status transitions.
- Worker threads must receive task-graph snapshots; only the scheduler thread may
  apply results, append events, or persist task graphs.
- Decomposition replay must bind every recorded decision to a deterministic
  node-context fingerprint and fail on context drift.
- Strategy comparison may report topology and outcome metrics but must not encode
  a hidden subjective winner policy in the scheduler or replay layer.
- Strategy ranking must use an explicit external judge policy with ordered,
  named objectives; ties must remain ties and ineligible outcomes unranked.
- Strategy elapsed time must be measured by the runner with a monotonic clock.
- Noisy strategy latency comparisons must use an explicit bounded odd sample
  count. `elapsed_ms` is the median; raw samples and MAD remain in evidence.
- Repeated samples for one strategy must preserve topology and outcome or the
  comparison must fail closed.
- Consolidation benchmarks must consume public runtime ports, use isolated
  workspaces, verify real outcomes, and keep cost/ranking policy external.
- Confidence analysis must aggregate immutable independent benchmark runs;
  repeated samples inside one run do not count as independent history.
- Benchmark history entries must share case, strategy set, and judge-policy
  fingerprint or confidence analysis must fail closed.
- Independent benchmark cases must keep separate history and confidence
  artifacts. Cross-case analysis may compare explicit strategy roles only,
  never raw case-specific rank or policy values.
- Cross-case role mappings must be explicit immutable policy. A measured role
  winner must never become an implicit scheduler routing rule.
- Token and cost metrics must come from an explicit typed usage provider, never
  inferred from task metadata or hidden provider pricing.
- Cost objectives require measured values with one comparable cost basis across
  eligible runs; missing or mixed measurements must fail closed.
- Timing and usage metrics must not alter topology or outcome fingerprints.
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
- Composite parent integration routes and ordered check plans must come from
  external policy, never task metadata.
- Typed integration selectors may inspect only admitted structural node fields;
  exact routes override ordered selectors, which override the default plan.
- Compound integration selectors may use only bounded explicit `all`/`any`
  groups; selector depth and node count must remain validated and finite.
- All-of integration composition must retain every check result and fail closed
  on exceptions or unknown verifier statuses.
- Plugin generation belongs behind `CapabilityAcquirer`, not inside the scheduler.
- Plugin family selection must come from external acquisition policy, never
  directly from task metadata or generated model output.
- Generated capabilities are available only while all admitted artifact hashes
  remain current. Acquisition does not authorize runtime invocation.
- Generated plugins marked as requiring an OS sandbox must fail closed when the
  configured sandbox backend is missing or unavailable; never fall back to a
  direct Python process.
- Production release validation must run the canonical sandbox release gate in
  strict mode and require status `passed` with every isolation check true.
- Canonical release validation must run pytest, wheel build/install/import, and
  the strict sandbox gate as independent bounded stages and retain every result.
- Canonical release runs must archive immutable versioned snapshots. Trend
  analysis must use a bounded prior window and deterministic thresholds.
- Release latency is a regression only when both configured relative and
  absolute thresholds are exceeded; status downgrades remain regressions.
- A failed release stage must not short-circuit later evidence collection.
- Composite release status may be `passed` only when every required stage
  passes; explicit sandbox degradation must remain visibly `degraded`.
- `--degraded-ok` may be used only for explicitly non-production validation and
  must remain visibly `degraded`, never `passed`.
- Sandbox mounts, network isolation, executables, and trust classification must
  come from external invocation policy, never task metadata.

## Testing

```bash
python -m pytest
python -m compileall -q .
```

Add tests for every new transition or terminal condition.

## Artifacts

Do not commit checkpoints, run logs, environments, caches, secrets, or build
outputs.
