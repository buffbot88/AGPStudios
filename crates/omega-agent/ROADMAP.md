# Roadmap — Ashat Neural Host Master Edition

**Server instance name:** Omega
**Project name:** Ashat Neural Host Master Edition
**Repo path:** `/home/opc/Projects/ashatneuralhost-master`
**Build details:** see `BUILDPLAN.md`
**Rules of engagement:** `VOWS.md` (protected)

---

## Goal

A universal-source Rust + Axum server with an always-on **LFM2.5-VL-450M intent router** (replaced the original 230M — see BUILDPLAN), a spawn-on-demand pool of up to three 1.2B **Coding Agent** instances, and a Vite + Phaser public telemetry canvas. The source must run identically on the public dev server and on the developer's local machine. No `.git`, `.gitignore`, or git artifacts live in this dev tree.

## Components (Phase 1 — this build)

| ID | Role | Lifecycle | Ports |
| --- | ----- | --------- | ----- |
| **VL-450M Router (Orchestrator)** | Intent classification (`chat/code/status/unknown`); correlates request ↔ 1.2B; routes responses back to caller. May scale under load. | Baseline always-on. Extras spawn-on-demand, kill-after-task. | `18079` (baseline); grows into `18078` → `18077`. (Total peak: 3×450M.) |
| **1.2B Coding Agent (a.k.a. BrainStem instance)** | Per-request inference. | Spawn-on-demand; killed after response lands with caller. | `18080` → `18081` → `18082` (cap = 3 by spec). |
| **BrainStem proxy** | Omega itself, listens on `:8080`. Routes through the orchestrator pool, then the coding-agent pool. | Always-on. | `0.0.0.0:8080`. |
| **Frontend (telemetry)** | Public canvas: 3 lane cards (Omega / Beta / Delta) + Generation Velocity chart. Polls `/api/{public_status, public_metrics, dashboard_timeseries}` every 8 s. | Dev: Vite on `:5173`. Prod: static `frontend/dist/` served by reverse proxy. | — |
| **Row chain — beta target** | Implemented in code, marked `enabled: false` in `server-config.json`. Enables in Phase 2. | Always-off (per config). | `127.0.0.1:8082`. |
| **Row chain — delta target** | Implemented in code, marked `enabled: false`. Enables in Phase 3. | Always-off (per config). | `127.0.0.1:8083`. |

## Failure policy

`503` is reserved for true OS-level failure (spawn fails repeatedly, out-of-memory) or queue-head ageing past `inference.timeout_seconds`. Both layers hold bounded FIFO queues and prefer patience over fail-fast.

## Build phases

| Phase | Scope | Status |
| ----- | ----- | ------ |
| **1** | Omega server baseline — Orchestrator pool, Coding Agent pool, telemetry frontend, row-chain wiring (beta/delta disabled). | **done** |
| **2** | `Beta` server (separate instance, port `8082`). Same code as Omega with role binding changed. Enables `row_chain[1]` from Omega. | **done** — seeded at `150.136.208.93:8082` via `scripts/seed_slave.sh`; cross-server routing live-tested (`lane: beta`) |
| **3** | `Delta` server (separate instance, port `8088`). Enables full `omega → beta → delta` row chain in Omega's config. | in progress (seed pending; OCI security list open) |
| **4** | Rate-limiting middleware (Tower layer before `auth`). | deferred (hook reserved) |
| **5** | Update propagation (`POST /api/admin/update`) — runs `scripts/seed_slave.sh` against every enabled `update.peers` entry so master builds propagate to Beta/Delta automatically. | **done** — auth-gated; live-tested against Beta |
| **6** | Advanced Coding Agent with tools — `Intent::Code` branch + `tool_loop.rs`: workspace-scoped tools (list/read/write/run/validate/skill), Script Validation Engine, `--mlock` tuning, hub completion countdown. | **done** |
| **7** | Ashat Hub / Chat Studio integration — `alpha_status.rs` reports the master status snapshot to the Hub (single point of incoming/outgoing traffic for the ecosystem); seeds Beta / Delta peers with updates. | deferred (seam reserved; `hub.enabled` default `false`) |
| **8** | MySQL skills DB — the router orchestrator seeds coding-agent workspaces from the Ashat skills base (`workspace.rs`, `workspaces/agent-{port}/`). The `skill` tool exists (Phase 6): enable `skills_db.enabled: true` and build with `--features omega-core/skills-db` once connection details land. | deferred (seam reserved) |
| **9** | GitHub self-updater — Ashat optimizes and updates her server from GitHub. | deferred (hook reserved) |

## Modular workspace (v2)

Phase 1 shipped as a single Cargo crate. v2 migrates the Rust side into a
workspace of three crates under `crates/`:

- `omega-common` — shared foundation (`types`, `config`, `models`, `metrics`, `log`, `workspace`)
- `omega-core` — inference engine (`demand`, `queue`, `orchestrator`, `proxy`, `router`, `supervision`)
- `omega-server` — axum binary `ashat-neural-host-master` (`main`, `handlers`, `auth`, `alpha_status`)

Binary name and public surface are unchanged. See `BUILDPLAN.md` → "Workspace
migration (v2)" for the locked decisions.

## Deferred hooks (seams reserved in Phase 1 code)

| Feature | Where it plugs in |
| ------- | ----------------- |
| Rate limiting | Tower middleware layer in `src/main.rs` **before** `src/auth.rs`. |
| Update propagation | `POST /api/admin/update` in `crates/omega-server/src/handlers.rs::admin_update`, gated by `auth`; runs `scripts/seed_slave.sh` per enabled `update.peers` (serialized; per-peer timeout + rollback handled by the script). GitHub pull/build still deferred. |
| Advanced Coding Agent with tools | New branch in `src/orchestrator.rs` returning `Intent::Code`; `src/proxy.rs` routes to a separate `src/tool_loop.rs` module (file created later, not in Phase 1). |

## Constraints / known trade-offs

- **Quantization.** GGUF Q4_K_M for both models (faster, less precise than the archived Q8_K). One-word intent-classifier output is robust at Q4_K_M but the precision delta is observable.
- **Intent router model.** The VL-450M router replaced the original 230M after live probing showed the 230M could not follow the single-word classification instruction (always said `chat`). See BUILDPLAN for the probe matrix.
- **Cold-start latency.** Every 1.2B spawn pays multi-second `llama-server` boot + 730 MB GGUF load; on a loaded 1-core host the health check window is 30 s and a failed spawn re-notifies waiters so the pump retries instead of stalling. Surfaced through `/api/public_metrics`.
- **Hard cap.** Spec ceiling is 3 concurrent 1.2B instances. The 4th concurrent caller queues, not rejects.
- **Baseline-resilience.** Baseline router respawn is critical-path: Omega's `8080` does not bind until the baseline reports `/health` ok. Spec-intent: never serve traffic if orchestrator is down.
- **Universal source.** `server-config.json` holds no host paths. Models auto-discover from `models/*.gguf`. `llama-server` is configurable (config → env → PATH).
- **No git.** This dev tree is not a git repo. `ROADMAP.md` and `BUILDPLAN.md` are intended to travel to GitHub later.
- **`ASHAT_KEY` continuity.** Carried over verbatim from the archived project so existing clients remain keyed without re-issuance.
- **`VOWS.md` integrity.** Protected by Vow 9 of `VOWS.md` itself. This build does not modify, rename, or delete `VOWS.md` at any point.

## Future host targets

| Host | Expectation |
| ---- | ----------- |
| Public dev server (current) | Source builds and runs; `models/` is local; `llama-server` installed system-wide. |
| Local machine | Mirror via universal source. `cargo build`, `npm install`, run. No host paths in source. |

## Validation surface

See `BUILDPLAN.md` "Validation" for the command-by-command verification path used at the end of this build.
