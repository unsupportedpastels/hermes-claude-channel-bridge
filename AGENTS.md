# Development contract

Build a standalone experimental Hermes model-provider plugin. Do not modify Hermes core, other profiles, default providers, credentials, or global Claude configuration. No commits, pushes, publication or live model calls unless the parent explicitly owns/authorizes that step.

- Hermes owns canonical history, skills, memory, tools and approvals. Claude's built-in task tools stay disabled. Never execute a model-proposed tool inside the bridge.
- Only documented Claude native interactive Channels/MCP interfaces. No `-p`, Agent SDK inference, borrowed OAuth credentials, request identity spoofing or fallback to another billing route.
- Per-session isolation; reject or re-bootstrap canonical history divergence without resurrecting stale context. Never replay an uncertain external action automatically.
- Native sessions are bounded, cancelable and cleaned up; no native process startup at plugin discovery. Development-channel consent must be explicit before live use.
- Runtime state, transcripts, account details and test receipts belong only under Git-ignored `.private/` or system temporary directories. Never include secrets in source/manifests/logs.
- Use focused RED/GREEN for deterministic protocol/lifecycle behavior. Offline tests first, then bounded real Hermes integration with temporary memory/skill stores. Test results must distinguish mocks from native execution.
- Python dependencies have bounded versions; JavaScript dependencies are pinned and locked. Keep tests and modules small; no speculative framework or core patches.
- Deliver only verified behavior; do not claim token streaming, indefinite idle, universal billing savings or full role equivalence.
