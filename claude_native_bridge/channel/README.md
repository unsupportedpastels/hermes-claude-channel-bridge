# Standalone yield-and-wait channel

Requires **Node 22**, a Unix private runtime directory, and the official MCP SDK
**1.30.0**. Installation is locked; tests use real SDK Clients and local child
servers, not Claude or any inference service.

```sh
cd channel
npm ci --ignore-scripts --no-audit --no-fund
npm test
```

## Launch contract

The owner creates a fresh absolute `HERMES_BRIDGE_RUNTIME_DIR`, owned by the
current user with no group/other permission bits (normally `0700`). Put
`{"token":"<cryptographically random bearer token>"}` in `transport.json`, normally
`0600`. Tokens must be nonempty printable ASCII without whitespace, at most 4096
characters. The bridge never generates or logs the owner's token.

Run `node /absolute/path/to/channel/server.mjs` as a **stdio MCP server**, with the
runtime environment variable set. Do not launch it as an HTTP-only daemon:
stdin must remain connected to the native MCP peer. Server identity is
`hermesbridge`; it advertises `experimental: {"claude/channel": {}}` and only the
`respond` tool. No proposed task tool is executed by this process.

HTTP binds only `127.0.0.1`, with an OS-selected port. **After MCP initialization**,
`ready.json` is atomically published with mode `0600` and exactly `{port,pid}`.
`bridge.lock` prevents simultaneous use of one runtime directory. Existing ready
or lock files cause startup refusal; never blindly reuse a failed runtime.

## Owner HTTP API

Every route requires `Authorization: Bearer <token>`, including status and unknown
paths. POST bodies require `Content-Type: application/json`. HTTP errors have
`{"error":"..."}`; responses are not cached.

1. First `POST /advance`:
   ```json
   {"ack":null,"request":{"request_id":"unique-1","content":"authoritative request"}}
   ```
   Returns `{"accepted":true}` and emits **one**
   `notifications/claude/channel` notification, whose `params.content` is the JSON
   string `{"request":{...}}` and `params.meta` is `{"request_id":"unique-1"}`.
2. The native peer calls `respond` with exactly one of:
   ```json
   {"request_id":"unique-1","kind":"tool_calls","tool_calls":[{"name":"some_hermes_tool","arguments":{}}]}
   ```
   ```json
   {"request_id":"unique-1","kind":"final","text":"answer"}
   ```
   This publishes `{sequence:1,...decision}` but **holds the MCP call pending**.
   Tool call arrays contain 1–16 entries. No extra keys are allowed in the decision
   or each tool-call entry; `arguments` is an arbitrary JSON object. `text` and
   `tool_calls` are mutually exclusive.
3. `GET /response?after=0` returns `{"response":<latest>|null}`. If no newer,
   unacknowledged decision exists, it waits at most **10000 ms**. Optional
   `wait_ms` is a canonical decimal integer in `0..10000`; e.g.
   `/response?after=0&wait_ms=500` permits frequent owner-side interruption checks,
   and `wait_ms=0` returns immediately. `after` is required, nonnegative, and cannot
   exceed the session sequence. Duplicate/unknown query keys are rejected.
4. Once Hermes has consumed the decision, `POST /advance` with its **exact**
   sequence and a **new** request ID:
   ```json
   {"ack":1,"request":{"request_id":"unique-2","content":"tool result or next task"}}
   ```
   This clears the retained decision and resolves the held MCP call with one text
   content block containing JSON `{"request":{...}}`. No additional channel
   notification is emitted. The same transition applies **after a final answer**.
   There is deliberately no separate acknowledge-only or close-final operation.

`GET /status` always returns exactly
`{sequence,current:<request_id|null>,held:<sequence|null>,failed:<error|null>}`.
While waiting for a decision `current` is its ID; after publication `current` is
null and `held` is the published sequence.

Malformed inputs return 400, unauthorized calls 401, stale/duplicate/overlapping
transitions 409, oversized bodies 413, wrong media types 415, waiter/reader
capacity exhaustion 429, and broken sessions 503. Unsupported route/method
combinations return 404/405. A rejected transition does not consume its ID or ack.

## Bounds and failure behavior

- HTTP JSON bodies are bounded to **8 MiB in bytes**, including chunked bodies,
  with a 15-second body deadline. Native stdio input has an **8 MiB SDK buffer
  limit**, including its JSON-RPC envelope/framing, so leave room for that overhead.
- Request IDs and proposed tool names are nonempty and at most 256 JS string code
  units. Up to 10000 unique request IDs are retained for replay detection. Reaching
  that limit fails the session rather than dropping old replay protection.
- Only the current request and latest unacknowledged decision are retained; no
  response history exists. There are at most 32 HTTP waiters, 32 body readers, and
  64 connections. Disconnecting a poll releases its listener and timer.
- A native cancellation while `respond` is held sets `failed: "native_cancelled"`,
  rejects the held promise, releases HTTP waiters with 503, and permanently
  refuses further transitions. Transport/protocol failures likewise fail closed.
  Cancellation after an already completed acknowledgement cannot corrupt the next
  request via the old call's removed cancellation listener.
- EOF, native transport closure, SIGINT, and SIGTERM clean pending work and owned
  ready/lock files, close HTTP/stdio, and exit with a one-second cleanup deadline.
  `transport.json` belongs to the owner and is not removed. SIGKILL cannot clean
  files; the owner must retire that runtime directory.
- The owner must bound native startup, overall session life, and tool-call/idle
  timeouts. A held call is intentionally not answered by a bridge heartbeat.
  Native-client timeout/cancellation behavior is **not** an indefinite-idle claim.
- Default diagnostics are silent. `HERMES_BRIDGE_DIAGNOSTICS=1` emits timestamped
  metadata-only JSON events to stderr, never prompts, outputs, IDs, or tokens.
  MCP protocol messages alone go to stdout.

## Verification scope

`node --test` covers the protocol state machine plus real SDK stdio handshake,
channel notification delivery, held results, exact ack correlation, replay and
shape rejection, next task after final, authenticated HTTP and byte limits,
short/default long polls, disconnect cleanup, cancellation, EOF, SIGTERM, and
unsafe runtime startup. RED→GREEN was observed for rendezvous transitions, native
cancellation, and the optional `wait_ms` extension. These are offline transport
and protocol tests, **not live Claude compatibility, billing, or inference tests**.
