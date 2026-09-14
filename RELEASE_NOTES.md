# Release-candidate notes

## Status

This is an **unpublished experimental candidate** released under the MIT License. No PyPI or other public-registry availability is claimed.

The package supports Python **>=3.11,<3.14**. The earliest Hermes host used for end-to-end testing was **0.21.2**, but that does not establish a minimum-compatible Hermes API. Current Hermes accepts `requires_hermes` comparator strings in `plugin.yaml`; the field remains omitted until a compatibility range is established.

## Candidate scope

- Standalone model-provider entry point; no Hermes core patch.
- Loopback authenticated OpenAI-compatible model discovery and Chat Completions subset.
- Native Claude Code through documented MCP Channels and local hooks; no print-mode or Agent SDK inference.
- Hermes remains authoritative for canonical history, tools, approvals, skills, and memory; Claude automatic memory is disabled in every bridge child.
- Owner/session isolation, bounded lifecycle, orphan retirement, fail-closed turn attribution, cancellation cleanup, and authenticated wakeups.
- Incremental text batches, correlated Stop-only final fallback, strict final sealing, bounded tool-call JSON, large-result paging, and safe usage provenance.
- Offline diagnostics that do not inspect login state or invoke inference by default.

## Packaging

The Python and channel package roots are aligned at candidate version **0.2.0**; the exact JavaScript dependency pins are unchanged. The Python metadata declares the SPDX license expression `MIT`. Wheel and source archives include the license, release notes, Python package, and required channel runtime assets. npm dependencies are pinned in `package-lock.json` but are not bundled as `node_modules`; checkout and installed-artifact workflows require `npm ci --ignore-scripts` in the channel directory. Run that command before the Python test gate because diagnostics validate the installed channel dependencies. Python installation and testing require a writable virtual environment, including writable `site-packages`.

## Verification boundary

- **Linux, latest source:** 298 Python tests passed with 3 host-specific skips; 35 Node tests passed with 1 host-specific skip. This is the current offline gate. Earlier native acceptance covered API/TUI text batches, Hermes tool execution, learning, and warm-session reuse, but the offline result does not itself invoke a model.
- **Windows 11:** 279 Python tests passed and 18 skipped; 31 Node tests passed and 5 skipped, alongside native ConPTY, Job Object, ACL, lifecycle, model-discovery, and installed-provider coverage. The tested revision predates the correlated Stop-only final fallback, so that later behavior is not claimed as Windows-verified.
- **macOS:** native acceptance passed 3 user prompts and 5 model calls in 17.64 seconds, covering an exactly-once tool effect, retained fact, controlled rotation, rotation metadata, a non-rotating follow-up, and verified cleanup. Rotation used a one-shot test cap derived from genuine prior status telemetry. This establishes lifecycle behavior, not that a natural production threshold was reached and not a universal throughput, rate-limit, allowance, or billing promise.
- Platform results are revision-specific. Offline tests do not prove native model availability, authentication, billing, or every target-platform lifecycle path.

No live inference, service operation, Hermes configuration change, publication, push, or commit was performed during this release-cleanup step.

## Context ownership and rotation

Claude automatic memory is disabled for every native child. Native automatic compaction is also disabled by default (`native_auto_compact: false`); Hermes canonical history remains the source used to bootstrap a replacement native session.

Between requests, the bridge uses Claude's correlated status-line counters to rotate at `rotation_percentage` (default 80% of the reported window), after subtracting configured `rotation_headroom_tokens`, with an optional `rotation_max_tokens` cap. Without correlated counters, `rotation_fallback_chars` bounds sent frames. A rotation occurs only between turns and re-bootstraps from Hermes canonical history. Opting into native automatic compaction remains an unverified recovery path.

## Model catalog boundary

The configured catalog is exactly:

- `claude-sonnet-5`
- `claude-opus-4-8`
- `claude-opus-5`
- `claude-haiku-4-5-20251001`
- `claude-fable-5-1`

Catalog presence is not a claim that every entry was live-tested or is available to every native account.

## Known limitations

- Text-only requests; multimodal input is unsupported.
- No native resume or attachment to arbitrary existing Claude sessions. Re-bootstrap from Hermes canonical history is a different operation.
- Incremental delivery uses native display batches, not token-level streaming. The correlated Stop-only path is a final-only fallback and does not claim incremental streaming.
- Direct MCP `read_result` paging is Claude-CLI-version-sensitive.
- Chat Completions subset only, `n=1`; native controls and role behavior are not complete OpenAI API equivalents.
- Usage counters and cache percentages are observability data, not rate, allowance, billing, or cost guarantees.

## License

Copyright (c) 2026 Claude Native Bridge contributors. Released under the [MIT License](LICENSE).
