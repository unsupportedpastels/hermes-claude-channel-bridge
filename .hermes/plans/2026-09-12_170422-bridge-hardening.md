# Claude Native Bridge — Hardening Plan (Sanitized)

The full original working plan, including machine-specific paths, private test receipts, session identifiers, account-specific measurements, and deployment notes, is preserved under Git-ignored `.private/`. This public copy retains only portable design and acceptance information.

## Goal and constraints

Harden the standalone Hermes model-provider plugin without modifying Hermes core. Native Claude Code remains the inference engine. Hermes owns canonical history, tools, approvals, skills, and memory; model-proposed task tools are never executed by the bridge.

Binding constraints from `AGENTS.md`:

- plugin-only implementation; no print-mode or Agent SDK inference
- isolated, bounded, cancelable native sessions
- no uncertain external-action replay
- private runtime state and receipts only in ignored or temporary locations
- pinned JavaScript dependencies and bounded Python dependencies
- offline deterministic tests before deliberate native acceptance
- no publication, service reconfiguration, or live inference without explicit authorization

## Baseline versus candidate

Prior native baselines covered ordinary Linux/macOS inference and Windows native process control. The current candidate has a Linux native acceptance record, while fresh macOS and Windows candidate runs remain pending. Earlier baselines must not be presented as current-candidate verification.

Hermes **0.21.2** is the earliest host used in end-to-end testing. This is not a compatibility minimum. The current manifest parser supports a comma-separated `requires_hermes` comparator string, but the plugin leaves that field unset until the supported range is established.

## Implemented hardening areas

### Large tool-result paging

Oversized individual or batched tool results are spooled beneath the bounded run directory and replaced by compact envelopes. The envelope carries correlation and paging information without embedding the oversized payload. Invalid or expired handles fail cleanly. The Node `read_result` tool and Python producer enforce matching path, size, and JSON bounds.

### Bounded bootstrap and model changes

When canonical history is too large for a native bootstrap, the bridge keeps a bounded recent tail and a clear omission marker. Runtime paging artifacts never become synthetic canonical user turns. Stable metadata precedes volatile data to preserve prompt-prefix reuse where possible.

### Lost-session recovery

A dead native process produces a typed failure for the uncertain in-flight request. The stale binding is retired so a later request can bootstrap a fresh native process from Hermes' canonical history. The failed request is not replayed automatically.

### Restart and orphan cleanup

API startup reconciles stale run directories and retires live orphan processes rather than pretending to reattach. Process identity checks are portable. Cleanup remains bounded and owner-scoped.

### Capacity and owner release

Closed or failed bindings are evicted before admission checks. Owner release retains ownership until teardown settles, preventing replacement work from racing cleanup. Delegated children receive isolated bindings through their Hermes session identifiers.

### Turn sealing, wakeups, and diagnostics

Native prompt identifiers bind display/stop events to the active request. Retired prompts cannot contribute output to later turns. Authenticated wake events reduce journal polling latency without becoming a second source of truth. Submission, response, and usage collection share an absolute exchange deadline. Diagnostic projections exclude prompts, response text, tokens, private paths, and raw account data.

### Compaction observation

Manual and automatic native compaction lifecycle events are observed and bounded. Missing completion or excessive generations fail closed. Summary payload content is not exposed in diagnostics. The current candidate still requires the live automatic-compaction acceptance gate before this path is declared supported.

## Delegation finding

Hermes delegation already creates child agents with distinct canonical session identifiers. The provider request hook forwards those identifiers, so children obtain isolated native bindings without new lane machinery. Operators must size the plugin's session limit for foreground plus child concurrency. The plugin must not silently change the user's delegation route.

A proposed same-prefix fan-out hold was dropped: delegated children have distinct goals/context and therefore distinct bootstrap prefixes. Adding a hold would add latency without a demonstrated consumer.

## Explicitly unsupported or deferred

- native resume or attachment to arbitrary pre-existing Claude transcripts
- multimodal input; the candidate is text-only
- token-level streaming beyond native display batches
- automatic compaction as a supported recovery path until the live gate passes
- full OpenAI endpoint, sampling, or native role-equivalence claims
- allowance, billing, or universal cost comparisons
- public distribution or license selection

## Release-candidate acceptance

1. Run the complete Python suite offline through a compatible Hermes checkout's test wrapper.
2. Run the complete locked Node suite offline.
3. Build wheel and source distribution into ignored `dist/`.
4. Inspect archive manifests and metadata; reject private state, runtime receipts, caches, tests not intended as runtime assets, and `node_modules`.
5. Verify the installed wheel contains the Python provider entry point plus channel runtime files, `package.json`, and `package-lock.json`; npm dependencies remain a separate locked install.
6. Record Linux native evidence separately from offline tests.
7. Keep macOS and Windows current-candidate native retests marked pending until run on those targets.
8. Do not publish until the owner chooses a release channel and license and a real Hermes compatibility range is established.
