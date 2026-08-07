# Omega

Server instance **Omega** of the **Ashat Neural Host Master Edition**.

Two-piece local LLM server:

- **230M Orchestrator** — always-on baseline on port `18079` carrying the smallest GGUF in `models/`, supervised by `supervision.rs`. Spawn-on-demand extras on `18078` / `18077` if load climbs and the baseline saturates.
- **1.2B Coding Agent** — spawn-on-demand pool on ports `18080` / `18081` / `18082`. Every spawned instance is killed once its generation has been returned to the caller (no long-lived 1.2B processes).

Both pools share the same `DemandPool` mechanism. Requests come into `:8080`, are classified by the 230M orchestrator, dispatched to a free 1.2B Coding Agent slot, streamed back to the caller, and the 1.2B instance is killed when the response lands.

`beta` (`:8082`) and `delta` (`:8083`) row-chain targets are wired in `server-config.json` but `enabled: false`. The walker is in place so flipping them on in Phase 2 / Phase 3 lights them up without code changes.

## Public surface

| Method | Path | Auth | Purpose |
| ------ | ---- | ---- | ------- |
| GET | `/health` | none | basic health + uptime |
| GET | `/api/public_status` | none | orchestrator + coding-agent snapshots (3 lanes) |
| GET | `/api/public_metrics` | none | rolling metrics summary + recent events |
| GET | `/api/dashboard_timeseries` | none | per-frame latency and rate data for the chart |
| GET | `/v1/models` | none | orchestrator + coding-agent labels |
| POST | `/v1/chat/completions` | `X-Ashat-Key` | OpenAI-compatible inference |

## Universal-source contract

This repo intentionally makes **no host-bound choices** so it can move between the public dev server and a developer laptop without modification:

- All model paths and the `llama-server` binary location are resolved at runtime from `server-config.json`, environment variables (`ASHAT_LLAMA_BIN`, `OMEGA_BIND`, `OMEGA_METRICS_PATH`, `ASHAT_PROJECT_ROOT`), or `PATH` lookup.
- GGUF files are auto-discovered from `models/*.gguf`. Hints in the config pin filenames when ambiguous; otherwise the orchestrator = smallest GGUF, the inference model = first `1.2B`-`Instruct` GGUF.
- The orchestrator binds port `18079`, `18078`, `18077`; the coding-agent binds `18080`, `18081`, `18082`. Adjust in `server-config.json` if a port is occupied on your host.

## Host setup

Install the **llama-server** binary from <https://github.com/ggerganov/llama.cpp> for your platform. Place it on `PATH` or point at it via `ASHAT_LLAMA_BIN`. Make sure both GGUF files are inside `models/`.

Then build and run:

```bash
# Server
cargo build --release
./target/release/ashat-neural-host-master

# Frontend
cd frontend
npm install
npm run dev          # http://127.0.0.1:5173 — proxies /api /health /v1 to :8080
npm run build        # production bundle → frontend/dist/
```

If `:8080` is in use, override the bind:

```bash
export OMEGA_BIND=0.0.0.0:9090
./target/release/ashat-neural-host-master
```

## Configuration keys (single ASHAT_KEY)

`server-config.json` holds a single `ASHAT_KEY` carried over from the archived project. Existing clients keep working without re-issuance. Override it once deployed:

```bash
jq '.ASHAT_KEY = "<new-key>"' server-config.json > server-config.json.new
mv server-config.json.new server-config.json
```

Never commit `server-config.json` to version control.

## Validation commands

```bash
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings
cargo test
cargo build --release

# Frontend
cd frontend
npm run typecheck
npm run build
```

## Files of interest

- `BUILDPLAN.md` — implementation order, validation, risks
- `ROADMAP.md` — high-level product phases
- `VOWS.md` — protected project guidance (do not modify)
