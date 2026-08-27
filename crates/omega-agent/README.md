# Omega

Server instance **Omega** of the **Ashat Neural Host Master Edition**.

Modular Cargo workspace: `omega-common` (shared foundation) → `omega-core`
(inference engine) → `omega-server` (axum binary `ashat-neural-host-master`).

Two-piece local LLM server:

- **350M text Orchestrator (intent router)** — always-on baseline on port `18079` carrying the router GGUF in `models/`, supervised by `supervision.rs`. Spawn-on-demand extras on `18078` / `18077` if load climbs and the baseline saturates.
- **1.2B Coding Agent** — spawn-on-demand pool on ports `18080` / `18081` / `18082`. Every spawned instance is killed once its generation has been returned to the caller (no long-lived 1.2B processes).

Both pools share the same `DemandPool` mechanism. Requests come into `:8080`, are classified by the 350M text orchestrator, dispatched to a free 1.2B Coding Agent slot, streamed back to the caller, and the 1.2B instance is killed when the response lands.

`beta` (`150.136.208.93:8082`) and `delta` (`129.213.147.225:8088`) row-chain targets are wired in `server-config.json` and **enabled**. Backends are chosen by weighted round-robin (omega 2 / beta 1 / delta 1) with concurrent health probes — a dead backend loses its share until it recovers. See `Beta_Delta.md` for peer access, seeding, and routing semantics.

## Public surface

| Method | Path | Auth | Purpose |
| ------ | ---- | ---- | ------- |
| GET | `/health` | none | basic health + uptime |
| GET | `/api/public_status` | none | orchestrator + coding-agent snapshots (3 lanes) |
| GET | `/api/public_metrics` | none | rolling metrics summary + recent events |
| GET | `/api/dashboard_timeseries` | none | per-frame latency and rate data for the chart |
| GET | `/v1/models` | none | orchestrator + coding-agent labels |
| POST | `/v1/chat/completions` | `X-Ashat-Key` | OpenAI-compatible inference |
| GET | `/` | none | landing page linking the public endpoints |
| POST | `/api/admin/update` | `X-Ashat-Key` (admin) | propagate the current build to Beta / Delta via `scripts/seed_slave.sh` |
| POST | `/api/admin/github_sync` | `X-Ashat-Key` (admin) | `{"mode": "status"\|"pull"\|"push"}` — verified GitHub sync via `scripts/github_sync.sh` |

## Universal-source contract

This repo intentionally makes **no host-bound choices** so it can move between the public dev server and a developer laptop without modification:

- All model paths and the `llama-server` binary location are resolved at runtime from `server-config.json`, environment variables (`ASHAT_LLAMA_BIN`, `OMEGA_BIND`, `OMEGA_METRICS_PATH`, `ASHAT_PROJECT_ROOT`), or `PATH` lookup.
- GGUF files are auto-discovered from `models/*.gguf`. Hints in the config pin filenames when ambiguous; otherwise the orchestrator = smallest GGUF, the inference model = first `1.2B`-`Instruct` GGUF.
- The text orchestrator binds port `18079`, `18078`, `18077`; the coding-agent binds `18080`, `18081`, `18082`. Adjust in your local `server-config.json` (a copy of the tracked `server-config.example.json`) if a port is occupied on your host.

## Host setup

A prebuilt **llama-server** binary ships at `bin/llama-server`; alternatively install it from <https://github.com/ggerganov/llama.cpp> and place it on `PATH` or point at it via `ASHAT_LLAMA_BIN`. Make sure the GGUF files are inside `models/` (the 350M text-router and 1.2B Instruct GGUFs).

Create your local config from the tracked template and set the keys:

```bash
cp server-config.example.json server-config.json
# then edit server-config.json: set ASHAT_KEY (add ASHAT_ADMIN_KEY for admin routes)
```

Then build and run:

```bash
# Server (workspace build — binary name is unchanged)
cargo build --release            # or: cargo build -p omega-server --release
./target/release/ashat-neural-host-master

```

This repository currently ships the server and API; use the HTTP endpoints listed above.

If `:8080` is in use, override the bind:

```bash
export OMEGA_BIND=0.0.0.0:9090
./target/release/ashat-neural-host-master
```

## Configuration keys

Your local `server-config.json` (gitignored copy of `server-config.example.json`) carries the shared `ASHAT_KEY` used by `X-Ashat-Key` on inference calls. It was carried over verbatim from the archived project so existing clients keep working without re-issuance. Override it once deployed:

```bash
jq '.ASHAT_KEY = "<new-key>"' server-config.json > server-config.json.new
mv server-config.json.new server-config.json
```

Admin routes (`POST /api/admin/update`, `POST /api/admin/github_sync`) use a dedicated admin key — set via the `ASHAT_ADMIN_KEY` environment variable or an `admin_key` config field. Until one is configured they fall back to the shared `ASHAT_KEY`.

`server-config.json` is gitignored — never push it (it embeds the live key and the slave `api_key`s). Commit config changes to `server-config.example.json` with placeholders instead.

## Validation commands

```bash
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings   # clippy not installed on the current dev host
cargo test
cargo build --release

```

## Files of interest

- `BUILDPLAN.md` — implementation order, validation, risks
- `ROADMAP.md` — high-level product phases
- `Beta_Delta.md` — Beta/Delta peer access, seeding, routing semantics, GitHub sync
- `CONTRIBUTING.md` — contribution rules + workflow
- `VOWS.md` — protected project guidance (do not modify)
- `server-config.example.json` — tracked config template (no secrets; copy → `server-config.json`)
- `scripts/` — `seed_slave.sh` (peer deploy), `github_sync.sh` (GitHub sync)
- `crates/omega-common` — shared types, config, models, metrics, `workspace.rs`
- `crates/omega-core` — demand pools, queues, orchestrator, weighted router, supervision, tool loop, skills DB
- `crates/omega-server` — axum handlers, auth, row-chain forwarding, and admin sync
