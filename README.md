# Claude Native Bridge

Hermes model-provider plugin that exposes native Claude Code through a loopback OpenAI-compatible Chat Completions API. Hermes keeps canonical history, tools, approvals, skills, and memory. The bridge never executes a model-proposed tool.

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

No Hermes core patch, print-mode inference, Agent SDK inference, copied Anthropic credential, or alternate billing route is used. This is an experimental, unpublished plugin; see `RELEASE_NOTES.md` for status and verification details.

## Requirements

- Python **>=3.11,<3.14** in a writable virtual environment (the one Hermes runs)
- Node **>=22,<23** with `npm` on PATH
- Native Claude Code, signed in with `claude auth login`
- `tmux` on Linux/macOS (Windows uses ConPTY)

## Install

Run from the repository root with the Python interpreter Hermes uses:

```sh
python -m pip install -e .
hermes plugins enable claude-native-bridge
hermes-claude-bridge setup --accept-development-channels
```

Setup does three things:

1. Installs the pinned channel dependencies with a locked `npm ci --ignore-scripts` if they are missing.
2. Starts the loopback API and writes a bridge-only bearer credential through Hermes' environment writer.
3. Saves the API port through Hermes' configuration writer.

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

With that key present, the first time Hermes creates a bridge client it runs the npm install and setup itself. Expect a slow first turn with no terminal output. Without the key, selecting the provider before setup fails with a message naming the setup command.

### Notes

- **Wheel install.** Replace the first line with `python -m pip install dist/hermes_claude_native_bridge-*.whl`; the wheel does not bundle `node_modules`, so setup still installs them.
- **`hermes plugins install <git-url>`** clones and enables the manifest only. It does not install the Python package or run setup, so it only replaces the `hermes plugins enable` step.
- **`--skip-channel-install`** makes setup fail instead of running npm. The manual equivalent is `npm --prefix <package>/claude_native_bridge/channel ci --ignore-scripts --no-audit --no-fund`.
- `python -m claude_native_bridge` is equivalent to the `hermes-claude-bridge` script.

## Commands

```sh
hermes-claude-bridge status
hermes-claude-bridge start
hermes-claude-bridge stop
hermes-claude-bridge doctor                      # offline prerequisite checks
hermes-claude-bridge doctor --check-cli-version  # also runs `claude --version`
```

Doctor does not inspect login state, start the API, or make model calls.

## Models

Discovered through `/v1/models`:

| Model | ID |
|---|---|
| Claude Sonnet 5 | `claude-sonnet-5` |
| Claude Opus 4.8 | `claude-opus-4-8` |
| Claude Opus 5 | `claude-opus-5` |
| Claude Haiku 4.5 | `claude-haiku-4-5-20251001` |
| Claude Fable 5.1 | `claude-fable-5-1` |

Availability, consent, authentication, and billing are native Claude responsibilities.

## Configuration

All keys live under `claude_native_bridge` in Hermes `config.yaml`.

- `max_sessions`: concurrent native sessions (foreground chats plus subagents).
- `rotation_percentage` (default 80), `rotation_headroom_tokens`, `rotation_max_tokens`: when Claude's own context counters reach the threshold between turns, the native session is retired and rebuilt from Hermes history within `bootstrap_max_chars`. Without counters, `rotation_fallback_chars` bounds the session instead.
- `native_auto_compact` (default `false`): opt back into Claude's automatic compaction. Unverified as a recovery path.

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
