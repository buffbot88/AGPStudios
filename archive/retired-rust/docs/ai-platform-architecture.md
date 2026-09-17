# AGP Studios AI Platform Architecture (ARCHIVED)

ARCHIVED — NOT CURRENT IMPLEMENTATION.

This document described an earlier architecture baseline. It is retained as
historical record only. It is not authoritative for the current AGP Studios
repo.

## Naming

- **Ashat** — the complete AI system.
- **Liquid Qwen** — Qwen coding inference plus LiquidAI 450M VL inference.
- **Alpha** — Ashat's Rust gateway and deterministic agent runtime.
- **Omega** — Ashat's dedicated inference host.
- **Galileo 3** — the user-facing coding workspace.

## System boundary

```text
User
  ↓
Galileo 3
  ↓
Alpha (:3000)
  ├─ sessions, permissions, workspace and agent loop
  ├─ structured tool execution and streaming events
  ├─ text proxy → Omega /v1/chat/completions
  └─ image proxy → Omega /v1/vision/chat/completions
        ↓
Omega (:8080)
  ├─ Qwen2.5-Coder-3B-Instruct Q4_K_M (primary coding/chat model)
  └─ LiquidAI 450M VL (vision model, internal worker)
```

Alpha performs no model inference and must not spawn `llama-server`. Omega is the only
inference host. Beta, Delta, peer routing, and the legacy Coding Agent are retired.

## Ownership

### Galileo 3

Owns the authenticated user experience, WebContainer workspace, editor, preview, terminal,
conversation UI, and rendering of structured events. It executes typed tools against its own
workspace and never selects a model or inference worker.

### Alpha

Owns sessions, authorization, request admission, deterministic image detection, the agent
loop, tool execution, abort propagation, bounded outputs, and the OpenAI-compatible proxy.
Alpha forwards inference to Omega and emits canonical events; it contains no model runtime.

### Omega

Owns inference only:

- `/v1/chat/completions` proxies to Qwen workers;
- `/v1/vision/chat/completions` proxies to the internal 450M VL worker;
- `/health`, `/ready`, and `/status` expose inference readiness;
- one Qwen worker is the safe current configuration on the 6 GiB host.

### Ashat Hub

Owns durable users, projects, snapshots, deployments, Admin CP, and operational control of
Omega. It does not execute Galileo tools or perform inference.

## Agent lifecycle

```text
User request
  → Alpha sends messages and typed tools to Qwen
  → Alpha returns text and structured tool calls
  → Galileo executes tools and sends bounded results back
  → Qwen chooses the next action or final response
```

Requests containing images are sent through Alpha to Omega's vision route; the resulting
visual context remains part of the same agent turn.

## Local development

```bash
cd /var/oled/data/AshatHub
cargo check --workspace
cd apps/ashat-hub-web && pnpm run build
cd ../galileo/apps/galileo && pnpm run typecheck && pnpm run test
```

Runtime configuration and model files are intentionally ignored by Git. Do not copy keys,
service environment files, databases, logs, `target/`, `node_modules/`, or model files into
the repository.

## Events

The canonical Galileo stream is SSE with `X-Galileo-Protocol: events`:

```text
response.start
text.delta
reasoning.delta
tool.start
tool.arguments
tool.result
status
error
response.complete
```

Tool identity and arguments are structured event data. Consumers must never parse assistant
text to discover tools. Events and logs must not contain credentials, prompts, or private
runtime data.

## Deployment

Galileo development is authoritative in the WebContainer. Explicit deployment captures a
snapshot in Ashat Hub; reload or close does not deploy automatically.

```text
edit → test → preview → explicit deploy → snapshot → publish
```

## Non-goals

- local Alpha inference;
- Liquid Thinking or Liquid 1.2B inference;
- Beta/Delta services;
- distributed peer inference or peer request routing;
- direct Galileo access to Omega workers.
