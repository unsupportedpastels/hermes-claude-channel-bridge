# Plugin-only API boundary

This replaces the custom host-side subprocess client with an actual authenticated loopback OpenAI-compatible service. Hermes core must remain unmodified. All live test inference uses Sonnet 5.

## Host side

The model-provider plugin advertises `auth_type=api_key`, a real loopback HTTP `/v1` base URL and an actual bridge-only bearer credential. It returns the ordinary OpenAI SDK client, adding an opaque per-client `X-Hermes-Bridge-Client` UUID header. Its existing request hook carries `hermes_session_id` in the request body. Neither this local credential nor client metadata is sent to Anthropic. `/v1/models` performs no inference.

An owner nonce plus the explicit conversation binding isolates foreground requests from forks, even when Hermes gives them the same session ID. Requests missing owner/binding information must be isolated, not merged by HTTP connection or guessed history similarity.

## API worker interface

Implement `create_app(token, home, engine_factory=None)` in `claude_native_bridge/api.py`. Default engine factory constructs `NativeBridgeClient(hermes_home=home)`. Expose authenticated `GET /health`, `GET /v1/models`, `POST /v1/chat/completions` and a bounded server CLI (`python -m claude_native_bridge.api_server --home ... --token-file ... --ready-file ... --port ...`). Bind only loopback. The parent owns installation, credential/config storage and service startup coordination.

The API dispatches to the internal engine using standard request arguments, `stream=False`, `extra_body={"hermes_session_id": supplied_id}` and an optional internal `_on_text(delta: str)` callback for streamed requests. Owner engines are never shared with another owner. Keep bounded request/owner lifetimes and close the engine on an active disconnected request. Idle clients can retain context for subsequent requests; prune them after a bounded interval. Native engine kwargs/settings remain independent from API service settings.

Responses are genuine ChatCompletion JSON. Stream responses use OpenAI SSE chunks for text, fully validated tool calls, finish reason, optional usage and `[DONE]`. Never dispatch partial tool arguments. Do not emit a completed answer twice if it was already streamed. Errors and incomplete generation must not become successful finishes. Usage may be null if unavailable. Bound JSON input to 8 MiB and avoid raw prompt/credential logs.

## Engine/stream worker interface

Extend `NativeBridgeClient.chat.completions.create` with internal `_on_text` callback support; default behavior remains usable without it. Pass callback through native exchange. Collect documented MessageDisplay batches and forward only text, validating session/request correlation and per-message batch order. The completion's message content must match text sent to the caller, including valid pre-tool prose, so canonical-history continuity survives.

Native ordinary-text finals use Stop as the final authority. Add a text-complete state to the local MCP bridge so the native session is retained and the next `/advance` starts a new channel input rather than restarting Claude. Preserve existing held respond tool flow for tool decisions and legacy structured finals. StopFailure remains an error. Streaming must not alter tool execution/approvals or invoke another model.

## Parent integration

Parent restores the installed Hermes core, changes provider/config setup to this actual API, adds service supervision, integrates existing Windows launcher code, packages dependencies, and verifies an unmodified host. No core monkeypatching, no fake API keys, no print/Agent-SDK inference, no default-model change, no commits or pushes without authorization.
