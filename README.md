# Claude Native Bridge

Experimental, unpublished Hermes model-provider plugin that exposes native Claude Code through a loopback OpenAI-compatible Chat Completions API. Hermes remains responsible for canonical conversation history, tools, approvals, skills, and memory. The bridge never executes a model-proposed task tool.

```text
Hermes
  OpenAI HTTP + SSE
        |
        v
Plugin-owned loopback API
  /health, /v1/models, /v1/chat/completions
        |
        v
Native Claude Code
  MCP Channels + local hooks
```

No Hermes core patch, print-mode inference, Agent SDK inference, copied Anthropic credential, or alternate billing route is used.

## Release-candidate status

This repository is a release candidate for local evaluation, not a published release. The earliest Hermes host used in end-to-end testing was **0.21.2**. That is a test datum, not a declared minimum-compatible Hermes API version. Current Hermes supports a `requires_hermes` manifest comparator, but this plugin intentionally leaves it unset until a compatibility range is established.

| Platform | Current evidence | Boundary |
|---|---|---|
| Linux | Latest source: **298 Python tests passed, 3 host-specific tests skipped; 35 Node tests passed, 1 host-specific test skipped**. Earlier native acceptance covered API/TUI text batches, a Hermes tool round-trip, learning, and warm-session reuse. | The latest source result is offline; it does not replace native inference evidence. |
| macOS | Native acceptance passed **3 prompts / 5 model calls in 17.64 seconds**, including tool continuity, controlled rotation, recall, a non-rotating follow-up, and cleanup. | Rotation used a one-shot test cap derived from real status telemetry. It proves lifecycle behavior, not a naturally reached production threshold or a universal request rate. |
| Windows 11 | Native platform coverage plus **279 Python tests passed, 18 skipped; 31 Node tests passed, 5 skipped**. | This tested revision predates the correlated Stop-only final fallback. It does not certify that later change on Windows. |

See `RELEASE_NOTES.md` for the verification boundary.

## Supported environment

- Python **>=3.11,<3.14**
- Node **>=22,<23**
- Native Claude Code, authenticated with its own `claude auth login`
- Hermes using the same Python environment in which this package is installed
- `tmux` on Linux/macOS; native Windows uses ConPTY and a parent-owned Job Object

The advertised model catalog is discovered through `/v1/models`:

| Model | ID |
|---|---|
| Claude Sonnet 5 | `claude-sonnet-5` |
| Claude Opus 4.8 | `claude-opus-4-8` |
| Claude Opus 5 | `claude-opus-5` |
| Claude Haiku 4.5 | `claude-haiku-4-5-20251001` |
| Claude Fable 5.1 | `claude-fable-5-1` |

These are the exact configured catalog entries, not a claim that every model was live-tested or is available to every native account. Model consent, authentication, entitlement, and billing remain native Claude responsibilities.

## Installation

Choose one Python installation mode. Neither mode installs JavaScript dependencies automatically. Use a writable virtual environment, including writable `site-packages`; installation and the Python test gate are not supported from a read-only or externally managed Python environment.

### From a checkout

Use an editable install while developing or evaluating a checkout:

```sh
python -m pip install -e .
npm --prefix claude_native_bridge/channel ci --ignore-scripts --no-audit --no-fund
hermes plugins enable claude-native-bridge
python -m claude_native_bridge setup --accept-development-channels
```

Run these commands from the repository root and use the Python interpreter used by Hermes.

### From a locally built wheel

A wheel installs the Python package, provider entry point, channel runtime modules, `package.json`, and `package-lock.json`. It does **not** bundle `node_modules`; install the locked npm dependencies after the wheel:

```sh
python -m pip install dist/hermes_claude_native_bridge-*.whl
CHANNEL_DIR="$(python -c 'from pathlib import Path; import claude_native_bridge; print(Path(claude_native_bridge.__file__).parent / "channel")')"
npm --prefix "$CHANNEL_DIR" ci --ignore-scripts --no-audit --no-fund
hermes plugins enable claude-native-bridge
python -m claude_native_bridge setup --accept-development-channels
```

A source distribution likewise requires a normal Python build/install followed by the locked npm install. No package is claimed to exist on PyPI or another public registry.

Setup starts the loopback API, writes a bridge-only bearer credential through Hermes' environment writer, and saves the API port through Hermes' configuration writer. It does not change the default model. Restart Hermes after setup, then select **Claude Native Bridge** in `/model` or run:

```sh
hermes chat --provider claude-native-bridge --model claude-sonnet-5
```

Service controls:

```sh
python -m claude_native_bridge status
python -m claude_native_bridge start
python -m claude_native_bridge stop
```

## API and lifecycle

All endpoints bind to loopback and require the generated bearer key except as documented by the local service bootstrap.

- `GET /health` reports API readiness, not vendor-login status.
- `GET /v1/models` discovers the configured catalog without inference.
- `POST /v1/chat/completions` supports validated text requests, Hermes tool calls, and optional SSE delivery.

The provider adds an opaque per-client owner header and passes Hermes' session binding through its request hook. Owner identity and canonical-history continuity prevent unrelated chats or review forks from sharing native context. Concurrent conflicts are rejected rather than interleaved. Active disconnects cancel only their associated engine; idle owners and native sessions retire on bounded timeouts.

`MessageDisplay` events provide incremental **text batches**, not token-level streaming. `Stop` seals final text; `StopFailure`, inconsistent prompt attribution, and incomplete streams remain errors. Proposed tools are returned to Hermes for approval and execution. Full tool arguments are validated before they are emitted.

Hook journals are the recovery source. Authenticated loopback wake events reduce polling latency without triggering inference, with bounded journal checks as fallback. Submission, response waits, and usage collection share an exchange deadline; startup and teardown have separate bounds. Repeated cancellation joins retained cleanup instead of starting duplicate teardown.

Tool proposals have matching Python/Node limits: 256 KiB encoded JSON, depth 32 with the root at zero, and 10,000 nodes. These limits do not apply to final text or canonical history. Oversized Hermes tool-result batches are replaced by compact paging envelopes and stored only in the bounded native run directory.

## Diagnostics

These checks do not inspect authentication or make model calls:

```sh
python -m claude_native_bridge doctor
python -m claude_native_bridge doctor --check-cli-version
```

The optional version check may launch the local Claude executable only to read its version. An unverified version warns; missing prerequisites fail. Doctor does not start the API or alter Hermes configuration.

API responses and final SSE chunks include `native_bridge_usage_provenance`, containing bounded source/correlation/model metadata and raw input, output, cache-read, and cache-creation counters when available. Unknown counters remain null. Cache percentage and token counters do not establish subscription allowance or comparative cost.

## Delegation

Hermes subagents use the bridge when the delegation route selects it:

```yaml
delegation:
  provider: claude-native-bridge
  model: claude-sonnet-5
```

Each child receives an isolated binding keyed by Hermes' canonical session identifier. Size `claude_native_bridge.max_sessions` for the intended foreground and child concurrency. This is an explicit deployment choice; the plugin does not change Hermes' default delegation route.

## Explicit limitations

- **Text only:** image, audio, and other multimodal request content is rejected.
- **No native-session resume:** the bridge does not attach to an arbitrary pre-existing Claude transcript or revive a terminated native process. Hermes can re-bootstrap a new native process from canonical history, but that is not native resume.
- **Claude automatic memory and automatic compaction are off.** Every native child sets `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`; Hermes remains the authoritative source for canonical history, memory, skills, tools, and approvals. By default each native session also launches with `autoCompactEnabled: false` and `DISABLE_AUTO_COMPACT=1`, and the bridge rotates instead. Before a continuation, it compares Claude's own status-line context counters with a threshold configured as `rotation_percentage` (default 80% of the reported window), minus `rotation_headroom_tokens`, with an optional `rotation_max_tokens` cap. At or above the threshold, the native session is retired and rebuilt from Hermes canonical history within `bootstrap_max_chars`; without correlated counters, `rotation_fallback_chars` of sent frames bounds the session instead. Rotation never interrupts a turn, so a single long turn can still grow past the threshold until it ends. Rotated completions carry `native_bridge_rotation`. Setting `native_auto_compact: true` opts back into native compaction, which remains unverified as a recovery path; compaction hooks stay registered as sentinels and an automatic compaction observed while disabled is reported as `native_bridge_unexpected_compaction`.
- **Batched display streaming:** incremental text can arrive before completion, but token-level streaming is not claimed.
- Chat Completions subset only; `n=1`. Unsupported request shapes fail explicitly.
- Native sampling and output controls are not token-exact OpenAI equivalents.
- Canonical role labels do not imply native Messages API role equivalence.
- Direct `read_result` paging depends on Claude CLI behavior. Ordinary Hermes spillover-file handling remains the preferred path for large tool results when available.
- Runtime state, credentials, transcripts, and test receipts stay in Git-ignored private or temporary locations.

## Verification

Offline gates:

```sh
npm --prefix claude_native_bridge/channel ci --ignore-scripts --no-audit --no-fund
/path/to/hermes-agent/scripts/run_tests.sh "$(pwd)/tests"
npm --prefix claude_native_bridge/channel test
```

The locked `npm ci --ignore-scripts` step is a prerequisite for the Python
suite because diagnostics validate the installed channel dependencies. Run the
Python gate with a writable virtual environment and writable `site-packages`.
Run the Python suite from a checkout against a compatible Hermes source tree;
some provider-contract tests import Hermes modules. `evals/` contains deliberate,
usage-consuming live harnesses. They are not ordinary unit tests and are not run
by installation or packaging. A green offline suite does not substitute for
the platform-specific native evidence and revision boundaries above.

## License

Released under the [MIT License](LICENSE).
