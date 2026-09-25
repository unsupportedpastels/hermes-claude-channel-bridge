# Release-candidate notes

## Status

This is an **unpublished experimental candidate** released under the MIT License. No PyPI or other public-registry availability is claimed.

The package declares no Python version. Hermes selects the interpreter for the provider adapter, and Hermes's package manager selects the one for the isolated server runtime. The offline suite has been run on Python 3.11 and 3.14. The earliest Hermes host used for end-to-end testing was **0.21.2**, but that does not establish a minimum-compatible Hermes API. Current Hermes accepts `requires_hermes` comparator strings in `plugin.yaml`; the field remains omitted until a compatibility range is established.

## Runtime isolation and service lifecycle

- **Cause fixed.** The API server was started as bare `sys.executable -m claude_native_bridge.api_server` with `PYTHONPATH` replaced by the plugin directory. Under a managed Hermes, dependencies reach the host's `sys.path` through the bootstrap, not through that interpreter, so the child failed with `ModuleNotFoundError: No module named 'yaml'`. Hermes then fell back to a client aimed at a listener that did not exist, and the failure showed up only as connection retries.
- **Server runtime.** Server-only packages now live in the plugin's own environment, built by Hermes's package manager (`pm.ensure_environment`), with a standard-venv fallback for hosts without it. They moved from `[project].dependencies` to the `server` extra, so they no longer enter Hermes's dependency resolution. The server starts isolated (`-I`), with only this package importable.
- **No installs on start.** Starting never installs. Setup, `repair`, or a first use with recorded development-channel consent prepares the runtime. A failed rebuild leaves the previous runtime selected. Startup failures now name the exception (for example the missing module) and the repair command.
- **Import-light registration.** Provider registration no longer imports the OpenAI SDK or pydantic. A launcher whose dependency generation does not match its interpreter (seen as `No module named 'pydantic_core._pydantic_core'`) still lists the provider.
- **Lifecycle.** The shared service exits after `claude_native_bridge_api.idle_exit_seconds` (default 300) once no live Hermes process (PID plus start time) holds an open bridge client and nothing is in flight. Clients open when created and close when Hermes closes them, so a permanent dashboard or gateway does not pin the service. `create_client` also refuses to launch on a loopback port already owned by another process. `stop` asks the service to drain and exit before falling back to identity-checked termination.
- **Limits.** There is no Hermes update hook, so automatic shutdown before an update is not guaranteed; `stop` remains the explicit pre-update step. If Hermes changes its pinned Python, the runtime needs `repair`, or it is rebuilt on first use when consent is recorded.

## Candidate scope

- Standalone model-provider entry point; no Hermes core patch.
- Loopback authenticated OpenAI-compatible model discovery and Chat Completions subset.
- Native Claude Code through documented MCP Channels and local hooks; no print-mode or Agent SDK inference.
- Hermes remains authoritative for canonical history, tools, approvals, skills, and memory; Claude automatic memory is disabled in every bridge child.
- Owner/session isolation, bounded lifecycle, orphan retirement, fail-closed turn attribution, cancellation cleanup, and authenticated wakeups.
- Incremental text batches, correlated Stop-only final fallback, strict final sealing, bounded tool-call JSON, large-result paging, and safe usage provenance.
- Offline diagnostics that do not inspect login state or invoke inference by default.

## Packaging

The Python and channel package roots are aligned at candidate version **0.2.0**; the exact JavaScript dependency pins are unchanged. The Python metadata declares the SPDX license expression `MIT`. Wheel and source archives include the license, release notes, Python package, and required channel runtime assets. npm dependencies are pinned in `package-lock.json` but are not bundled as `node_modules`; checkout and installed-artifact workflows require `npm ci --ignore-scripts` in the channel directory. The `hermes-claude-bridge setup` console script (equivalent to `python -m claude_native_bridge setup`) runs that locked install itself when the pinned dependencies are missing, and provider client creation fails with a setup pointer until both the credential and the channel dependencies exist, unless `claude_native_bridge.development_channels_accepted: true` is already recorded in config, in which case the first client creation runs the npm install and setup itself. Run the npm step before the Python test gate because diagnostics validate the installed channel dependencies. Hermes' plugin installer registers and enables the manifest only; the provider is loaded through the pip entry point, so the Python package must still be installed into Hermes' interpreter. Python installation and testing require a writable virtual environment, including writable `site-packages`.

## Verification boundary

- **Linux, latest source:** 298 Python tests passed with 3 host-specific skips; 35 Node tests passed with 1 host-specific skip. This is the current offline gate. Earlier native acceptance covered API/TUI text batches, Hermes tool execution, learning, and warm-session reuse, but the offline result does not itself invoke a model.
- **Windows 11:** 279 Python tests passed and 18 skipped; 31 Node tests passed and 5 skipped, alongside native ConPTY, Job Object, ACL, lifecycle, model-discovery, and installed-provider coverage. The tested revision predates the correlated Stop-only final fallback, so that later behavior is not claimed as Windows-verified.
- **macOS:** native acceptance passed 3 user prompts and 5 model calls in 17.64 seconds, covering an exactly-once tool effect, retained fact, controlled rotation, rotation metadata, a non-rotating follow-up, and verified cleanup. Rotation used a one-shot test cap derived from genuine prior status telemetry. This establishes lifecycle behavior, not that a natural production threshold was reached and not a universal throughput, rate-limit, allowance, or billing promise.
- Platform results are revision-specific. Offline tests do not prove native model availability, authentication, billing, or every target-platform lifecycle path.
- **Runtime-isolation revision, Linux (Jarvis, Ubuntu 24.04, Hermes 0.21.5):**
  - Offline suite: 383 passed and 3 skipped on Python 3.14. On Python 3.11, 381 passed; the same 2 tests fail with and without this change, because of a Hermes import under that legacy venv.
  - Node: 38 passed, 1 skipped.
  - `evals/runtime_durability.py`, run in a throwaway home under a bare managed interpreter:
    - The old launch reproduces the missing-`yaml` failure.
    - A real PM runtime is built, and every server module imports inside that runtime's own prefix.
    - The server maps no application-environment files.
    - A live client keeps the server up; it retires about 18 s after its last client exits.
    - Cold restart works, and stop is graceful.
    - An A→B runtime upgrade works, and a failed upgrade keeps B.
    - Two homes stay isolated.
  - No model call was made. **Windows and macOS were not exercised for this revision.** Their code paths (Windows venv redirector argv match, graceful stop in place of `TerminateProcess`, PM on those hosts) are untested there.

No live inference, service operation, Hermes configuration change, publication, push, or commit was performed during this release-cleanup step.

## Context ownership and rotation

Claude automatic memory is disabled for every native child. Native automatic compaction is also disabled by default (`native_auto_compact: false`); Hermes canonical history remains the source used to bootstrap a replacement native session.

Between requests, the bridge uses Claude's correlated status-line counters to rotate at `rotation_percentage` (default 80% of the reported window), after subtracting configured `rotation_headroom_tokens`, with an optional `rotation_max_tokens` cap. Without correlated counters, `rotation_fallback_chars` bounds sent frames. A rotation occurs only between turns and re-bootstraps from Hermes canonical history. Opting into native automatic compaction remains an unverified recovery path.

## Model catalog boundary

The configured catalog is exactly:

- `claude-sonnet-5`
- `claude-opus-4-8`
- `claude-opus-5`
- `claude-opus-5-5`
- `claude-haiku-4-5-20251001`
- `claude-fable-5-1`

Catalog presence is not a claim that every entry was live-tested or is available to every native account.
Opus 5.5 requires Claude Code 2.1.280 or later. Opus 4.8 remains independently selectable.

## Known limitations

- Text-only requests; multimodal input is unsupported.
- No native resume or attachment to arbitrary existing Claude sessions. Re-bootstrap from Hermes canonical history is a different operation.
- Incremental delivery uses native display batches, not token-level streaming. The correlated Stop-only path is a final-only fallback and does not claim incremental streaming.
- Direct MCP `read_result` paging is Claude-CLI-version-sensitive.
- Chat Completions subset only, `n=1`; native controls and role behavior are not complete OpenAI API equivalents.
- Usage counters and cache percentages are observability data, not rate, allowance, billing, or cost guarantees.

## License

Copyright (c) 2026 Claude Native Bridge contributors. Released under the [MIT License](LICENSE).
