# Standalone yield-and-wait channel

Private transport used by the Claude Native Bridge. It requires Node **>=22** and the pinned MCP SDK **1.30.0**. Tests use real SDK clients and local child servers; they do not authenticate Claude or invoke inference.

From a checkout:

```sh
npm --prefix claude_native_bridge/channel ci --ignore-scripts --no-audit --no-fund
npm --prefix claude_native_bridge/channel test
```

A Python wheel/sdist includes the channel runtime, `package.json`, and `package-lock.json`, but not `node_modules`. Locate the installed `claude_native_bridge/channel` directory and run the same locked npm install there.

## Launch contract

The Python owner creates a fresh absolute `HERMES_BRIDGE_RUNTIME_DIR` with owner-only access before writing configuration or secrets. On POSIX this is normally mode `0700`; transport files are normally `0600`. On Windows the owner applies user-scoped ACLs. Put:

```json
{"token":"<cryptographically random bearer token>"}
```

in `transport.json`. Tokens must be nonempty printable ASCII without whitespace and at most 4096 characters. The channel never generates or logs the owner's token.

Run `node /absolute/path/to/channel/server.mjs` as a **stdio MCP server** with the runtime environment variable set. Do not launch it as an HTTP-only daemon: stdin must remain connected to the native MCP peer. The server identity is `hermesbridge`; it advertises `experimental: {"claude/channel": {}}` and exposes only bridge transport tools. It never executes a proposed Hermes task tool.

After MCP initialization, `ready.json` is atomically published with `{port,pid}` and owner-only access. HTTP binds only `127.0.0.1` on an OS-selected port. `bridge.lock` prevents concurrent use of one runtime directory. Existing ready or lock files cause startup refusal; a failed runtime must be retired rather than blindly reused.

## Owner HTTP API

Every route, including status and unknown paths, requires `Authorization: Bearer <token>`. POST bodies require `Content-Type: application/json`. Errors use `{"error":"..."}` and responses are not cached.

1. First `POST /advance`:
   ```json
   {"ack":null,"request":{"request_id":"unique-1","content":"authoritative request"}}
   ```
   It returns `{"accepted":true}` and emits one `notifications/claude/channel` notification with a JSON-string `params.content` and matching request metadata.
2. The native peer calls `respond` with either tool calls or final text:
   ```json
   {"request_id":"unique-1","kind":"tool_calls","tool_calls":[{"name":"some_hermes_tool","arguments":{}}]}
   ```
   ```json
   {"request_id":"unique-1","kind":"final","text":"answer"}
   ```
   The decision is published with the next sequence while the MCP call remains held. Tool-call arrays contain 1–16 entries. Extra keys are rejected; `text` and `tool_calls` are mutually exclusive.
3. `GET /response?after=0` returns `{"response":<latest>|null}`. Without a newer unacknowledged decision it waits at most 10,000 ms. `wait_ms` is an optional canonical decimal integer in `0..10000`. `after` is required, nonnegative, and cannot exceed the session sequence. Duplicate or unknown query keys are rejected.
4. After Hermes consumes a decision, the next `POST /advance` supplies its exact sequence and a new request ID. This clears the retained decision and resolves the held MCP call with the next request. The same transition follows a final answer; there is no acknowledge-only or close-final operation.

`GET /status` returns exactly `{sequence,current:<request_id|null>,held:<sequence|null>,failed:<error|null>}`.

Malformed inputs return 400, unauthorized calls 401, stale/overlapping transitions 409, oversized bodies 413, wrong media types 415, capacity exhaustion 429, and broken sessions 503. Unsupported route/method combinations return 404/405. Rejected transitions do not consume IDs or acknowledgements.

## Bounds and failure behavior

- HTTP JSON bodies are bounded to 8 MiB in bytes, including chunked bodies, with a 15-second body deadline. Native stdio input shares the SDK's 8 MiB buffer with JSON-RPC framing, so callers must leave envelope headroom.
- Request IDs and proposed tool names are nonempty and at most 256 JavaScript string code units. Up to 10,000 unique request IDs are retained; exhausting the replay set fails closed.
- Only the current request and latest unacknowledged decision are retained. Limits are 32 HTTP waiters, 32 body readers, and 64 connections. Disconnecting a poll releases its listener and timer.
- Native cancellation while `respond` is held permanently fails that transport, rejects the held promise, and releases HTTP waiters with 503. Old cancellation listeners cannot corrupt a later acknowledged transition.
- EOF, transport close, and supported termination signals clean pending work and owned ready/lock files with a bounded deadline. Forced process termination can leave files; the owner must retire the runtime directory.
- The owner bounds startup, session life, tool waits, and idle time. A held call is not answered by a bridge heartbeat; indefinite idle is not supported.
- Default diagnostics are silent. `HERMES_BRIDGE_DIAGNOSTICS=1` appends timestamped metadata-only events to `channel-diagnostics.log` in the private runtime directory, never prompts, output text, identifiers, or tokens, and never to stderr (a CLI persists MCP stderr in its own log, outside the private runtime). MCP protocol messages alone use stdout.

## Verification scope

`node --test` covers protocol transitions, real SDK stdio handshake, channel notification delivery, held results, exact acknowledgement correlation, replay/shape rejection, post-final continuation, authenticated HTTP and byte limits, polling/disconnect cleanup, cancellation, process shutdown, and unsafe runtime startup. These are offline transport tests, not live Claude compatibility, billing, or inference tests.

The current Linux channel suite passes 35 tests with 1 host-specific skip. The recorded Windows run passes 31 tests with 5 platform skips; that tested revision predates the correlated Stop-only final fallback. macOS live acceptance passed 3 prompts and 5 model calls in 17.64 seconds with a controlled one-shot rotation cap. That cap verifies rotation lifecycle behavior, not a naturally reached production threshold or a universal request rate. Offline channel tests do not establish native model availability, billing, or inference behavior.

## License

Released under the MIT License; see the repository `LICENSE` file.
