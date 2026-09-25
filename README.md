# Claude Native Bridge

Hermes model-provider plugin that exposes native Claude Code through a loopback OpenAI-compatible Chat Completions API. Hermes keeps canonical history, tools, approvals, skills, and memory. The bridge never executes a model-proposed tool.

```text
Hermes
  OpenAI HTTP + SSE
        |
        v
Plugin-owned loopback API
  /health, /v1/models, /v1/chat/completions
  (isolated server runtime, separate from Hermes's Python environment)
        |
        v
Native Claude Code
  MCP Channels + local hooks
```

No Hermes core patch, print-mode inference, Agent SDK inference, copied Anthropic credential, or alternate billing route is used. This is an experimental, plugin; see `RELEASE_NOTES.md` for status and verification details.

## Requirements

- Hermes itself. The plugin declares no Python version: Hermes chooses the interpreter for the adapter, and Hermes's package manager chooses the one for the server runtime.
- Node **>=22** with `npm` on PATH
- Native Claude Code, signed in with `claude auth login`
- `tmux` on Linux/macOS (Windows uses ConPTY)

## Install

Run from the repository root with the Python interpreter Hermes uses:

```sh
python -m pip install -e .
hermes plugins enable claude-native-bridge
hermes-claude-bridge setup --accept-development-channels
```

Setup does four things:

1. Installs the pinned channel dependencies with a locked `npm ci --ignore-scripts` if they are missing.
2. Prepares the isolated server runtime (see below) and checks that every server module imports in it.
3. Starts the loopback API and writes a bridge-only bearer credential through Hermes' environment writer.
4. Saves the API port through Hermes' configuration writer.

Restart Hermes, then choose **Claude Native Bridge** in `/model` or run:

```sh
hermes chat --provider claude-native-bridge --model claude-sonnet-5
```

Setup does not change the default model.

### Skipping the setup command

The setup line exists because Hermes has no post-install hook and because driving Claude Code's development Channels needs explicit consent. To record that consent in config instead, add this to Hermes `config.yaml` before the first use:

```yaml
claude_native_bridge:
  development_channels_accepted: true
```

With that key present, the first time Hermes creates a bridge client it runs the npm install and setup itself, and it also rebuilds a missing or outdated server runtime. Expect a slow first turn with no terminal output. Without the key, selecting the provider before setup fails with a message naming the setup command.

### Server runtime

The API server does not run on Hermes's Python packages. Its dependencies (FastAPI, uvicorn, jsonschema, and on Windows pywinpty/pywin32/pyte) are installed into a separate environment under `<Hermes home>/claude-native-bridge/server-environment/`, built by Hermes's package manager (`pm.ensure_environment`) with the Python that Hermes pins. They are not in the plugin's install requirements, so they never enter or constrain Hermes's own dependency set.

The server starts in isolated mode (`python -I`), with only this package made importable. Ambient `PYTHONPATH`, `VIRTUAL_ENV`, user site-packages, and the host's dependency generation cannot leak into it. Starting the server never installs anything: a missing or broken runtime is reported with the command that fixes it. On hosts without Hermes's package manager, `repair` falls back to a standard virtual environment built from Hermes's base interpreter.

A failed rebuild keeps the previous runtime selected. A running server keeps the runtime it started with until it next restarts.

### Notes

- **Wheel install.** Replace the first line with `python -m pip install dist/hermes_claude_native_bridge-*.whl`; the wheel does not bundle `node_modules`, so setup still installs them.
- **`hermes plugins install <git-url>`** clones and enables the manifest only. It does not install the Python package or run setup, so it only replaces the `hermes plugins enable` step.
- **`--skip-channel-install`** makes setup fail instead of running npm. The manual equivalent is `npm --prefix <package>/claude_native_bridge/channel ci --ignore-scripts --no-audit --no-fund`.
- `python -m claude_native_bridge` is equivalent to the `hermes-claude-bridge` script.

## Commands

```sh
hermes-claude-bridge status
hermes-claude-bridge start
hermes-claude-bridge stop                        # drains, then exits; forced only as a fallback
hermes-claude-bridge repair                      # rebuild/validate the server runtime only
hermes-claude-bridge doctor                      # offline prerequisite checks
hermes-claude-bridge doctor --check-cli-version  # also runs `claude --version`
```

Doctor does not inspect login state, start the API, run the server runtime, or make model calls. `repair` changes no credential, port, consent, or model setting.

## Models

Discovered through `/v1/models`:

| Model | ID |
|---|---|
| Claude Sonnet 5 | `claude-sonnet-5` |
| Claude Opus 4.8 | `claude-opus-4-8` |
| Claude Opus 5 | `claude-opus-5` |
| Claude Opus 5.5 | `claude-opus-5-5` |
| Claude Haiku 4.5 | `claude-haiku-4-5-20251001` |
| Claude Fable 5.1 | `claude-fable-5-1` |

Availability, consent, authentication, and billing are native Claude responsibilities.
Opus 5.5 requires Claude Code 2.1.280 or later. Opus 4.8 remains independently selectable; choosing Opus 5.5 does not replace it.

## Configuration

All keys live under `claude_native_bridge` in Hermes `config.yaml`.

- `max_sessions`: concurrent native sessions (foreground chats plus subagents).
- `rotation_percentage` (default 80), `rotation_headroom_tokens`, `rotation_max_tokens`: when Claude's own context counters reach the threshold between turns, the native session is retired and rebuilt from Hermes history within `bootstrap_max_chars`. Without counters, `rotation_fallback_chars` bounds the session instead.
- `native_auto_compact` (default `false`): opt back into Claude's automatic compaction. Unverified as a recovery path.
- `retain_diagnostics` (default `false`): keep each native session's private run directory (request-window state, driver markers, spooled results) after teardown, for offline diagnosis.
- A private, always-on metadata journal lives at `<Hermes home>/claude-native-bridge/api/events.jsonl`. It rotates at 1 MiB with three backups (up to 4 MiB total). Events identify admission failures, generation start/completion/failure, error class, fixed reason code, an opaque run directory, and a short trace linking API and native failures. It never records prompts, tool arguments, bearer tokens, or arbitrary exception text. Use `events.jsonl`, then `.1` through `.3` for older entries.
- `channel_diagnostics` (default `false`): pass `HERMES_BRIDGE_DIAGNOSTICS=1` to the channel server. Each session's private `channel-diagnostics.log` rotates at 128 KiB with two backups. It records sequence/branch metadata and rejected simple tool names, never request content or tool arguments. Nothing is written to channel stderr, which Claude may persist outside the private runtime. Combine with `retain_diagnostics` to keep these per-session files after teardown; **retained run directories themselves are not rolled**.

Under `claude_native_bridge_api`:

- `port`: the loopback API port that setup chose.
- `idle_exit_seconds` (default `300`, `0` = never): how long the shared server may sit unused with no live Hermes client before it exits. Each Hermes process identifies itself by PID and process start time, so a reused PID never counts as a live client. The server stays up while any identified client process is alive or any request is in flight. The next client starts it again.

To route Hermes subagents through the bridge:

```yaml
delegation:
  provider: claude-native-bridge
  model: claude-sonnet-5
```

## How it works

- Every endpoint binds to loopback and requires the generated bearer key.
- Each Hermes client gets an isolated native session keyed by owner and session identifier. Conflicts are rejected rather than interleaved; idle sessions retire on bounded timeouts.
- Text arrives as incremental display batches, not token-level streaming. Proposed tool calls are returned to Hermes for approval and execution; tool arguments are validated first (256 KiB, depth 32, 10,000 nodes).
- Oversized tool results are replaced by paging envelopes stored in the bounded run directory.
- Claude automatic memory and automatic compaction are disabled in every native child; rotation replaces compaction.
- Responses include `native_bridge_usage_provenance` with raw token counters when available. These are observability data, not allowance or cost guarantees.
- Registering the provider imports only Hermes's provider interface. The OpenAI SDK loads when a client is created, so the provider stays listed even if a launcher cannot load the SDK's native pydantic extension.

## Updating Hermes

The server runs from its own runtime, not from Hermes's application environment, so it does not hold the files a Hermes dependency update replaces. It exits by itself once no Hermes process is using it. There is no Hermes update hook to stop it right away, so if an updater still reports it, run `hermes-claude-bridge stop` first. Do not use `--force-venv`. After the update, the next client starts it again. If Hermes's pinned Python changed, run `hermes-claude-bridge repair` (or let first use rebuild it when consent is recorded).

## Limitations

- Text only; multimodal content is rejected.
- No resume of pre-existing Claude transcripts; Hermes re-bootstraps a new native session from its own history.
- Chat Completions subset only, `n=1`; sampling controls are not token-exact OpenAI equivalents.
- A single long turn can grow past the rotation threshold, since rotation only happens between turns.
- Runtime state, credentials, and transcripts stay in Git-ignored private or temporary locations.

## Development

```sh
npm --prefix claude_native_bridge/channel ci --ignore-scripts --no-audit --no-fund
/path/to/hermes-agent/scripts/run_tests.sh "$(pwd)/tests"
npm --prefix claude_native_bridge/channel test
```

The npm step must run before the Python suite because diagnostics validate the installed channel dependencies. Some tests import Hermes modules, so run against a compatible Hermes source tree. `evals/` holds live, usage-consuming harnesses that are not part of the offline suite.

## License

Released under the [MIT License](LICENSE).

## Maintainer

[unsupportedpastels](https://github.com/unsupportedpastels)
