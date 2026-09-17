# AGP Studios

AGP Studios is the monorepo for **Ashat**, the complete AI coding system.

## Architecture

```text
Alpha Rust agent runtime → Omega inference host
                                      ├─ Qwen2.5-Coder-3B-Instruct
                                      └─ LiquidAI 450M VL
```

- `apps/ashat-hub-web/` — Ashat Hub web and Admin CP.
- `crates/ashat-hub/` — durable platform, authentication, projects, deployments, and admin APIs.
- `crates/alpha-common/` — Alpha request and event types.
- `crates/alpha-core/` — Alpha admission, routing, and Omega proxy logic.
- `crates/alpha-server/` — Alpha gateway (`:3000`).
- `crates/omega-agent/` — Omega inference proxy and worker supervisor (`:8080`).
- `docs/ai-platform-architecture.md` — current system ownership and topology.
- `docs/ai-platform-contracts.md` — current API, event, tool, and security contracts.

Alpha performs no inference. Omega hosts Qwen for coding/chat/tool calls and LiquidAI 450M VL
for image understanding. Beta, Delta, peer routing, and Liquid Thinking are retired.

## Local development

```bash
cd /var/oled/data/AshatHub
cargo check --workspace
cd apps/ashat-hub-web && pnpm run build
```

Production services and model paths are runtime-managed. Model files, credentials, service
environment files, logs, databases, `target/`, `node_modules/`, and generated runtime data
must not be committed.

## Storage boundary

Runtime code remains on the root filesystem; `/var/oled/pcp` is reserved for Oracle
Performance Co-Pilot data.
