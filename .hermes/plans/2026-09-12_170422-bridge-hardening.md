# Claude Native Bridge — Hardening Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Fix the reliability and cost gaps found during real use of the installed `claude-native-bridge` plugin, and verify that subagent delegation rides the bridge correctly with zero new machinery.

**Architecture:** All work stays inside the plugin at `/home/mark/nerdspeak/hermes-claude-channel-bridge` (installed to `~/.hermes-dashboard/plugins/claude-native-bridge/`). No Hermes core edits. Every feature is plugin-owned config plus protocol changes on the existing MCP channel server (`respond` tool gains a sibling `read_result` tool; the OpenAI-compatible API gains no new endpoints). Native Claude Code remains the only inference engine; per-session bindings keep Hermes owning history, tools, skills, memory, and approvals.

**Tech Stack:** Python 3.11 (plugin package `claude_native_bridge`), Node/MCP channel server (`server.mjs`), pytest (41 existing Python tests) + node test runner (25 existing tests), tmux-launched native `claude` sessions.

**Project contract (from AGENTS.md, binding):** no core edits, no `-p`/SDK inference, per-session isolation, bounded and cancelable native sessions, runtime state only under Git-ignored paths or system temp, RED/GREEN tests with mocks distinguished from native execution, pinned JS deps, deliver only verified behavior.

---

## Current verified baseline (do not regress)

- Real Sonnet 5 round-trip through the API: `BRIDGE_OK` in ~13s.
- Streaming via `MessageDisplay` hook: text arrives ~9s before completion.
- Stock `/model` picker lists 5 models; stock OpenAI provider path discovers them.
- Usage draw matches native interactive (3 points vs 10 on the benchmark).
- Config `command: /home/mark/.local/bin/claude` fixes the dashboard-PATH launch bug (verify the same guard exists on macOS; the Mac install used a LaunchAgent with its own PATH).

## Phase 1 — Correctness fixes (must-have)

### Task 0: Baseline commit (needs explicit Mark authorization)

**Objective:** The repo is currently entirely untracked and AGENTS.md forbids commits without explicit authorization. Land a verified baseline before any task touches files, so every later diff is reviewable.

**Step 1:** Ask Mark to authorize the initial commit.
**Step 2:** Run both test suites first (`python -m pytest tests/ -q`, `npm --prefix claude_native_bridge/channel test`) and record the counts in the receipt — this also replaces the unverified "41/25" figures.
**Step 3:** Commit everything (plugin source, plan, AGENTS.md) as the baseline. Per-task commits follow only within this authorization scope.

### Task 1: `read_result` paging for oversized tool results

**Objective:** A tool result larger than ~20k chars no longer fails the exchange — and never destroys the session's rendezvous state.

**Design correction from the live review (session 20260912_171259_249805):** oversized results don't merely fail the exchange; the harness truncates-and-spools the whole payload **including the request id**, so the session loses its rendezvous and cannot recover. The design must separate the small envelope from the bulky payload:

- The tool-result envelope (handle name, request id, size, paging hint) must be **small enough to never be truncated** — synthesize it as a compact replacement result, never inline the payload.
- The full payload is spooled to the **run-directory file path** (not env, not server memory — the store fills per exchange, so server-start injection can't work; the run dir is where `native.py` already writes per-session state).
- `read_result(handle, offset, length)` in `server.mjs` reads pages from that spool file; the file lives and dies with the run directory (expires on `native.close()`).
- Invalid/expired handle → clean error text ("handle expired; re-run the tool"), never a protocol fault.

**Files:**
- Modify: `claude_native_bridge/channel/server.mjs` (add `read_result` MCP tool reading the run-dir spool)
- Modify: `claude_native_bridge/client.py` (`_create_sync` tool-result interception: envelope synthesis + spool write)
- Modify: `claude_native_bridge/native.py` (allow `mcp__hermesbridge__read_result` in `--allowedTools`)
- Test: `tests/test_paging.py`, `channel/paging.test.mjs`

**Step 1: Write failing test (client).**
```python
def test_oversized_tool_result_becomes_handle():
    # fake Hermes tool result of 69_000 chars
    # engine returns decision proposing tool call; driver returns huge result
    # second exchange content must contain "read_result" handle text, not the payload
    ...
```
Run: `python -m pytest tests/test_paging.py -v` → FAIL (no paging).

**Step 2: Implement envelope + spool.** In `client.py`, when a tool result exceeds `settings.page_threshold` (new setting, default 20_000 chars): write the full text to `<runtime>/spool/<handle>.txt` and replace the tool result with the compact envelope:
```
Result too large (69,412 chars). Handle: r7f3.
Call read_result(handle="r7f3", offset=0, length=15000) to read it in pages.
```
**Invariant under test:** the envelope (with request id and handle) must survive even when the raw result would have been truncated — that is the fix, not an optimization.

**Step 3: Implement `read_result` in `server.mjs`.** Validates handle + bounds against the run-dir spool file, returns the requested slice. Add `mcp__hermesbridge__read_result` to allowed tools.

**Step 4: Handle expiry/cleanup.** Spool lives in the run directory; expires with `native.close()`. Invalid handle → clean error text ("handle expired; re-run the tool"), never a protocol fault.

**Step 5: Tests green both layers.** `python -m pytest tests/test_paging.py -v` → PASS; `npm --prefix claude_native_bridge/channel test` → PASS.

**Step 6: Native smoke (bounded).** One live Sonnet 5 call with a 60k-char tool result; verify ≥2 page requests and a grounded final answer. Distinguish in the receipt: this step used native execution.

### Task 2: Size-aware bootstrap on provider/model switch

**Objective:** Switching GPT → Fable mid-conversation with a huge history bootstraps bounded recent context + summary instead of one giant first message.

**Files:**
- Modify: `claude_native_bridge/client.py` (history-tracker reset path)
- Modify: `claude_native_bridge/settings.py` (`bootstrap_max_chars`, default 100_000)
- Test: `tests/test_bootstrap.py`

**Step 1: Failing test** — history of 700k chars + model change → first frame content ≤ `bootstrap_max_chars` and includes an explicit truncation notice. Run → FAIL.

**Step 2: Implement.** On model/effort switch with history > threshold: keep the newest tail that fits, prepend `"[Earlier conversation omitted: N chars. Ask read_result handle 'boot' for older windows if needed.]"`, and register the omitted prefix as a paged handle reusing Task 1's store.

**Step 3: Tests green.** Unit tests for threshold math, tail selection, handle registration.

### Task 3: Stale-native-session rebuild

**Objective:** A wedged native session fails deterministically into a clean re-bootstrap on the next request, not an opaque 502 loop.

**Files:**
- Modify: `claude_native_bridge/client.py` (exception classification in `_create_sync`)
- Modify: `claude_native_bridge/native.py` (`health()` — cheap liveness check)
- Test: `tests/test_rebuild.py`

**Step 1: Failing test** — native process killed mid-exchange → next `create()` call automatically starts a fresh native with history re-prepared (no stale binding reuse), and the error surfaced to Hermes on the *first* failed call is a clear, typed exception. Run → FAIL.

**Step 2: Implement.** Classify native errors: `process-dead` → close binding, reset history tracker, raise typed `NativeSessionLost` (Hermes retries normally). Never replay an uncertain in-flight external action (contract).

**Step 3: Tests green + one bounded native kill-test.**

### Task 4: Restart reconnect / orphan prevention

**Objective:** API-server restart either reattaches to a live native session or kills it cleanly at startup; no orphaned `claude` processes.

**Files:**
- Modify: `claude_native_bridge/api_server.py` (startup sweep in `main()` — single owner of this behavior)
- Modify: `claude_native_bridge/supervisor.py` (portable pid-liveness reconciliation)
- Test: `tests/test_orphans.py`

**Step 1: Failing test** — start server, create binding, kill server, restart: startup sweep finds the run directory with a live native PID, and either (a) re-arms the binding, or (b) terminates the native process and archives the run. Decision: **(b) kill-and-archive** — simpler and honest, since Hermes-side history makes re-bootstrap cheap. Run → FAIL.

**Step 2: Implement sweep** at `api_server.main()` start: for each run dir in `<home>/claude-native-bridge/runs/`, if native PID alive → graceful `native.close()` equivalent (SIGTERM to tmux session, then process group), mark run archived.

**Step 3: Tests green.** PID-liveness check must be **portable** (macOS has no `/proc`): use `psutil` if already a dependency, else `os.kill(pid, 0)` + verify the process identity via `ps` command output (check argv contains `claude`), never bare `/proc/<pid>/cmdline`. The sweep lives only in `api_server.main()` — drop the `ensure_server` reference from the Files list.

### Task 5: Slot release on native idle-close (rescoped)

**Objective:** When a native session idle-closes itself, the client's binding slot is released too — no orphaned slot, no second idle knob.

**Rescope from the live review:** `native.py` already tracks last-used time and closes itself after `settings.idle_timeout` — do **not** add `idle_evict_seconds` or a parallel clock in the client. The actual gap: after the native self-closes, `client.py`'s binding dict still holds the slot (the existing scan only evicts `native.closed` bindings when *that* binding is next touched, and only if `native is not None`).

**Files:**
- Modify: `claude_native_bridge/client.py` (binding scan treats `native is None or native.closed` as evictable; evict before the capacity check, not after)
- Test: `tests/test_eviction.py`

**Step 1: Failing test** — binding whose native has idle-closed (`native.closed` True, injected via fake clock/native) → next `admit` for a *different* key evicts it and admits the new session instead of raising "session limit reached". Also: `native is None` bindings (failed engine creation) are evictable. Run → FAIL.

**Step 2: Implement.** Widen the existing `expired` scan condition and move it before the capacity raise. No new settings.

**Step 3: Tests green.**

### Task 6: Config + Mac parity check

**Objective:** Real-use config for Mark's setup; verify the Mac install doesn't have the PATH bug.

**Step 1:** Set in `~/.hermes/config.yaml` (both Linux and Mac): `max_sessions: 6`, keep `command: /home/mark/.local/bin/claude` (Linux) / absolute path discovered on Mac. **Must run before Task 7** — the live default is 2, and Task 7's parent+child test would hit the cap. Record which config file is canonical (`~/.hermes/config.yaml`; the dashboard config mirrors it) in the README.
**Step 2:** On mark-mac over SSH: confirm the LaunchAgent's `PATH` includes the claude dir or `command:` is absolute; run one Sonnet 5 smoke through `127.0.0.1:63590/v1`. Report receipts; do not "fix" what isn't broken.
**Step 3:** Aux routing check (cheap, not a build): count ephemeral `aux-` calls in a day of logs. If material, record the finding in the plan's Open Questions — routing aux to a cheap provider is Hermes-side config, not plugin code.

## Phase 2 — Subagent delegation through the bridge (verify, don't build)

**Source-verified finding (2026-09-12):** Hermes delegation already provides everything lanes needed — no new lane machinery. No opt-in flag; routing happens wherever the delegate model is selected (config `delegation.provider`/`delegation.model`, or per-call provider override).

Evidence:
- `tools/delegate_tool.py::_build_child_agent` — each child is a real `AIAgent` with its **own session DB entry and own `session_id`** (not the parent's).
- `agent/chat_completion_helpers.py:2047` — every API call passes `session_id=agent.session_id` into `provider_profile.build_extra_body(...)`. The bridge's `ClaudeAPIProfile.build_extra_body` forwards that as `hermes_session_id`, so each child binds to its **own native session automatically**: warm across the child's turns, isolated from the parent, released when the child ends.
- `Owners.admit` (api.py) keys on client header + `hermes_session_id`, with per-owner busy/409 protection — parallel children cannot interleave into one native session.

### Task 7 (revised): Live delegation verification + capacity math

**Objective:** Prove the existing path end-to-end and size `max_sessions` for real use.

**Step 1:** Temporarily set `delegation.provider: claude-native-bridge` + `delegation.model: claude-sonnet-5` in `~/.hermes/config.yaml` (revert after — **mandatory**, per standing rule that children use the cheaper delegate model).

**Step 2:** Run one bounded `delegate_task` ("reply with the word READY") from a live session. Verify:
- Child's run directory distinct from parent's (`runs/session-*` count, distinct `native-pid.json` PIDs).
- Child's turns reuse ONE native session (one `native-usage.json`, ≥1 cache-read on turn 2).
- Parent's native binding untouched by the child's calls.
- Slot released: `max_sessions` headroom restored after child exit.

**Step 3:** Document the recipe (README): "to run subagents on native Claude, set `delegation.provider`"; capacity math: foreground sessions + max_concurrent_children ≤ `max_sessions` (6 covers 2 foreground + 3 children + headroom).

**Step 4:** Revert delegation config to `gpt-6-astra`. Mandatory, not optional.

## Phase 3 — Cache nice-to-have (downgraded: premise partly false)

### Source check on Task 8 (fan-out prefix hold)

The 5-second hold copies native Claude Code *workflow fan-outs*, where same-prefix agents share a prompt. **Hermes delegation cannot produce same-prefix children**: `_build_child_system_prompt` embeds each child's distinct `goal`/`context` in the system prompt (delegate_tool_progress.py:149), so every child's bootstrap prefix differs → nothing to share → the hold would add 5s latency and save zero tokens in the delegation case. It could only help same-prompt parallel callers of the raw API, which is not our pattern.

**Decision: DROP Task 8.** Record the native mechanism here for reference if a same-prefix use case ever appears (e.g., batch runner with identical prompts). Revisit only with a concrete consumer — a hook with no consumer is speculative infrastructure (root AGENTS.md rubric).

## Explicitly out of scope

- Streaming token-level delivery beyond current `MessageDisplay` batches.
- Approval-relay UI (Hermes already gates tools; denial flows back as a normal tool error — verify in Task 3 tests that denial ≠ protocol fault).
- Aux-provider routing changes (Hermes-side config; measure first per Task 6).
- Any change to billing, metering, or token accounting.

## Verification / acceptance

- Full suites: `python -m pytest tests/ -q` and `npm --prefix claude_native_bridge/channel test` green on every task.
- One end-to-end receipt per phase: Phase 1 = oversized-session review (69k real transcript) completes through the TUI; Phase 2 = one delegate task on the bridge with child native PID distinct from parent's.
- ~~Phase 3 = fan-out measurement file.~~ Dropped (premise false; see Phase 3).
- Mac smoke repeated after Phases 1–2 merge.
- Live native runs bounded: timeouts, ≤5 sessions, usage caps agreed before each.

## Risks / tradeoffs / open questions

- **Paging changes tool-result semantics** — Hermes sees the synthesized handle text in canonical history. Mitigation: keep the full result recorded in run diagnostics (off by default, `retain_diagnostics`).
- **Bootstrap threshold is a heuristic** — 100k chars default; wrong for some workflows. Config-exposed; document.
- **Restart kill-and-archive** discards warm native cache on every API restart (Hermes desktop restarts are rare; accepted).
- **Amendments from the live bridge review (session 20260912_171259_249805):** Task 0 added (baseline commit, pending Mark authorization); Task 1 redesigned around the truncation envelope (request id + handle must never ride in a truncatable payload); Task 5 rescoped to slot-release (native.py already self-closes on idle — no second knob); Task 4 made portable (no `/proc`-only checks) and single-owned in `api_server.main()`; Task 6 ordering fixed (must precede Task 7, live `max_sessions` default is 2); Task 7 revert made mandatory.
- ~~**Open:** does Hermes delegation construct one OpenAI client per child (stable header) or per call?~~ **Resolved:** child session IDs are stable and reach `build_extra_body` on every call (`chat_completion_helpers.py:2047`); owner keying is safe regardless of header churn.
- ~~**Open:** fan-out prefix hold.~~ **Resolved:** premise false for delegation — child prefixes always differ (goal/context embedded in each child's system prompt). Task 8 dropped.
- **Open:** measure aux-call frequency (Task 6) before deciding whether aux routing needs plugin support at all.
