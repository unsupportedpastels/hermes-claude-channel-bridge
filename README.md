# Claude Native Bridge — plugin-only OpenAI API

Hermes uses its normal OpenAI-compatible HTTP client. This plugin runs an authenticated local API and delegates inference to a persistent native Claude Code session. **No Hermes core patch is required.** The earlier experimental picker changes have been reverted from both the installed core and the separate picker worktree.

```text
Unmodified Hermes
  tools / approvals / skills / memory
             |
             | OpenAI HTTP + SSE
             v
Plugin-owned loopback API
  /v1/models, /v1/chat/completions
             |
             | MCP Channels + local hooks
             v
Native Claude Code
  existing claude.ai login; no print/Agent-SDK inference
```

## Verified

Live tests use **Sonnet 5**:

- Standard OpenAI SDK model discovery and streamed generation through the real API.
- Text arrived before completion; the real Hermes TUI displayed partial output about nine seconds before the final answer.
- A follow-up reused the same native session; context and cache counters appeared in the TUI.
- Unmodified Hermes executed a real terminal tool round-trip through the API.
- Memory reading/writing, skill loading/updating, background-review memory/skill updates, and fresh-session memory recall passed through the API using isolated stores.
- The stock `/model` picker displayed all five catalog entries, without core edits.

The advertised catalog is:

| Model | ID |
|---|---|
| Sonnet 5 | `claude-sonnet-5` |
| Opus 4.8 | `claude-opus-4-8` |
| Opus 5 | `claude-opus-5` |
| Haiku 4.5 | `claude-haiku-4-5-20251001` |
| Fable 5.1 | `claude-fable-5-1` |

IDs were checked against official model documentation. Account availability and consent remain native Claude responsibilities; only Sonnet 5 is used for current live tests. Haiku does not receive an unsupported effort flag.

## Platform status

| Platform | State |
|---|---|
| Linux | Live API, TUI, streaming, tools, learning and native-session reuse verified |
| macOS | Native launcher/process lifecycle tests passed on a real Mac; end-to-end native inference still requires Claude Code login |
| Windows | Native ConPTY/job/ACL backend integrated; native-only tests still require an unlocked Windows desktop and a valid native Claude login |

Windows support is not WSL. ConPTY is used for background console control and startup consent, not for carrying model task data. The package does not claim full native Windows validation while its native-only tests remain unexecuted.

## Installation and use

Requirements: Hermes with its Python environment, native Claude Code, Node 22+, and tmux on Linux/macOS. Windows-only Python dependencies are declared in `pyproject.toml`. Claude authenticates through its own `claude auth login`; no Anthropic credential is copied into this plugin.

Install this repository as a standalone model-provider plugin under the intended Hermes home's `plugins/claude-native-bridge`, or link a local checkout there. Install the declared Python dependencies in the environment used by Hermes, then install channel dependencies:

```sh
npm --prefix claude_native_bridge/channel ci --ignore-scripts
hermes plugins enable claude-native-bridge
python -m claude_native_bridge setup --accept-development-channels
```

Run the Python command from the plugin directory or with the package installed, using Hermes' Python environment. No built-in tool override permission is needed.

Setup starts the local API, stores a real bridge-only API key through Hermes' environment writer, and saves the API port through Hermes' configuration writer. It does not change the default model. `--home /path/to/hermes-home` explicitly targets another installation. CLI/Desktop homes that intentionally share a config share the API endpoint; each home still needs its own saved environment credential and installed plugin entry.

Restart the Hermes process after setup, then use `/model` → **Claude Native Bridge**, or select explicitly:

```sh
hermes chat --provider claude-native-bridge --model claude-sonnet-5
```

When resuming an older pre-API bridge session, reselect the provider/model to replace its obsolete `claude-native://` runtime route. Do not delete the conversation to migrate it.

## API and lifecycle

All endpoints require the generated local bearer key. The server binds only to loopback.

- `GET /health`: API service readiness, not a vendor-login claim.
- `GET /v1/models`: catalog discovery without inference.
- `POST /v1/chat/completions`: text, validated tool calls and optional live SSE.

The plugin supplies the ordinary OpenAI SDK client with an opaque per-client owner header and passes Hermes' session binding through a supported request hook. Owner identity plus canonical-history continuity prevents foreground chats and review forks from sharing the wrong native context. Unbound requests use isolated one-shot engines. Do not use HTTP connection identity as conversation identity.

The API never executes proposed task tools. Hermes executes them, applies its approvals, and returns results in its next request. Native Claude's task tools are disabled.

`MessageDisplay` supplies real text batches. `Stop` confirms final text; `StopFailure` stays an error. After ordinary text completes, the native session remains available for the next channel request. Incomplete or inconsistent streams do not become successful completions. Full tool arguments are validated before being emitted to Hermes.

Repeated completed requests can replay the cached response; concurrent conflicts are rejected rather than starting duplicate inference. Active disconnects close only their associated engine. Idle native sessions and API owners retire on bounded timeouts. The API service itself remains available until stopped:

```sh
python -m claude_native_bridge status
python -m claude_native_bridge start
python -m claude_native_bridge stop
```

Stopping the API stops its active native work. A later provider request may start the service again. Startup and stop operations share a lifecycle lock.

## Usage and limitations

- Native per-call input/output/cache counters are captured through a local status-line hook and returned as standard API usage fields. Missing or ambiguous counters stay unknown.
- Cache percentage is not subscription allowance. The earlier prototype's benchmark matched the user's native three-point draw; that is not a universal billing guarantee, and the full API refactor has not been subjected to another large allowance A/B.
- This is a supported Chat Completions subset, not every OpenAI endpoint or option. Text-only input and `n=1`; unsupported request shapes fail explicitly.
- Native sampling/output limits are not a complete OpenAI generation-control equivalent. Do not treat a requested token cap as a guaranteed native token-exact stop.
- Native Claude still has its own instruction hierarchy. Role-labelled canonical frames are not a claim of native Messages API role equivalence.
- Existing native Claude sessions retain their own normal transcripts. Plugin runtime state and credentials are private; account-specific test receipts are confined to Git-ignored `.private/`.

## Verification

```sh
/path/to/hermes-agent/scripts/run_tests.sh /absolute/path/to/plugin/tests
npm --prefix claude_native_bridge/channel test
```

`evals/api_live.py` verifies actual API/SSE and an unmodified-Hermes tool round-trip. `evals/api_learning.py` verifies learning through the API. These consume native model usage and write only isolated test stores. Run them deliberately, not as ordinary unit tests. The earlier direct-client harnesses are historical development probes, not the current API setup path.

No core monkeypatching, borrowed vendor tokens, print-mode fallback, commits, pushes or public release are part of this installation.

## Running subagents on native Claude (delegation)

Hermes subagents ride the bridge automatically when the delegate route points
at it. In `~/.hermes/config.yaml` (the canonical config; the dashboard config
mirrors it):

```yaml
delegation:
  provider: claude-native-bridge
  model: claude-sonnet-5
```

Each child gets its own native session (own session ID -> own
`hermes_session_id` binding), warm across its turns and released when the
child ends. Capacity: foreground sessions + `max_concurrent_children` must
stay <= `claude_native_bridge.max_sessions` (6 covers 2 foreground + 3
children with headroom). This is a deliberate per-use setting; the default
delegation route stays on the cheaper provider.

## Large tool batches and session identity

Paging applies to both an individually oversized tool result and a contiguous
batch whose combined text exceeds `page_threshold`. Every result in an
over-limit batch is spooled under the native run directory and replaced by a
small, deterministic `read_result` envelope before the channel event is built.
This prevents the aggregate-message overflow seen when several individually
small tool results arrived together. Duplicate/overlapping `respond` calls
remain fail-closed; the fix prevents the lost-rendezvous state rather than
making uncertain tool calls replayable.

Every bound native frame also receives a stable synthetic metadata message
containing the canonical Hermes session ID. It explicitly marks bridge runtime
directory names as opaque, so the model never has to infer session identity
from `session-*` folders. Bounded model-switch bootstraps preserve this metadata
outside the truncatable history tail.

## Claude Code compatibility

The ordinary native bridge, streaming, Hermes tools, and lifecycle are verified
with Claude Code 2.1.270 on Linux and macOS. Direct `read_result` MCP paging is
verified with 2.1.269 on Linux. In 2.1.270, Claude may omit the auxiliary
`read_result` tool (macOS) or produce conflicting MessageDisplay batches during
a synthetic paged-result run (Linux). Normal Hermes oversized results still
work through Hermes' spillover-file + `read_file` path; session
`20260912_195531_0486be` completed the formerly failing 69k-result review.
Until the Claude CLI behavior is resolved, the Linux bridge can pin
`claude_native_bridge.command` to the installed 2.1.269 binary when direct MCP
paging is required. Do not pin macOS to 2.1.269 after credentials have been
refreshed by 2.1.270; normal 2.1.270 operation is the verified Mac route.
