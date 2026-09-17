# AGP Studios AI Platform Contracts (ARCHIVED)

ARCHIVED — NOT CURRENT IMPLEMENTATION.

This document described an earlier contract baseline. It is retained as
historical record only. It is not authoritative for the current AGP Studios
repo.

## Request boundary

```text
Galileo 3 → Alpha (:3000) → Omega (:8080)
```

Galileo calls Alpha only. Alpha owns authorization, admission, deterministic image detection,
tool execution, abort propagation, output caps, and the agent loop. Alpha performs no
inference.

## Omega API

- `POST /v1/chat/completions` — Qwen2.5-Coder-3B-Instruct text, coding, and tool calls.
- `POST /v1/vision/chat/completions` — LiquidAI 450M VL image understanding.
- `/health`, `/ready`, `/status` — Omega readiness and worker status.

The 450M VL process is internal to Omega. Workers are never selected directly by Galileo.

## Canonical events

With `X-Galileo-Protocol: events`, Alpha emits canonical SSE records:

```text
response.start     { response_id }
reasoning.delta    { delta }
text.delta         { delta }
tool.start         { id, name }
tool.arguments     { id, arguments }
tool.result        { id, ok, result?, error? }
status             { state, message? }
error              { code, message, retryable }
response.complete  {}
```

Tool calls are structured events. No consumer may parse assistant text, embedded JSON,
prefixes, or regular expressions to find tools.

## Tool execution

Galileo executes typed tools against its own workspace. Read-only tools, file actions, and
commands are bounded and abortable. Repeated-call protection and iteration limits belong to
Alpha's agent loop. Model inference never executes tools directly.

## Vision

Alpha detects image content without model routing and forwards the request to Omega's vision
route. Vision output remains part of the same Galileo agent turn and can be followed by Qwen
tool calls.

## Data and security

Ashat Hub owns users, projects, snapshots, deployments, and Admin CP records. Runtime
credentials, cookies, provider keys, model files, logs, databases, and service environment
files stay outside Git and are never returned in events, API responses, browser storage, or
logs.

## Local development

```bash
cd /var/oled/data/AshatHub
cargo check --workspace
cd apps/ashat-hub-web && pnpm run build
cd ../galileo/apps/galileo && pnpm run typecheck && pnpm run test
```

Do not commit generated assets, `target/`, `node_modules/`, model files, or runtime data.

## Retired

Beta, Delta, peer-fleet inference, Liquid Thinking, Liquid 1.2B, local Alpha inference, and
the legacy Omega action protocol are not part of the active contract.
