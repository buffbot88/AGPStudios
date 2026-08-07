# Roadmap — Ashat Neural Host Master Edition

**Server instance name:** Omega
**Project name:** Ashat Neural Host Master Edition
**Repo path:** `/home/opc/Projects/ashatneuralhost-master`
**Build details:** see `BUILDPLAN.md`
**Rules of engagement:** `VOWS.md` (protected)

---

## Goal

A universal-source Rust + Axum server with an always-on 230M **Orchestrator**, a spawn-on-demand pool of up to three 1.2B **Coding Agent** instances, and a Vite + Phaser public telemetry canvas. The source must run identically on the public dev server and on the developer's local machine. No `.git`, `.gitignore`, or git artifacts live in this dev tree.

## Components (Phase 1 — this build)

| ID | Role | Lifecycle | Ports |
| --- | ----- | --------- | ----- |
| **230M Orchestrator** | Intent classification; correlates request ↔ 1.2B; routes responses back to caller. May scale under load. | Baseline always-on. Extras spawn-on-demand, kill-after-task. | `18079` (baseline); grows into `18078` → `18077`. (Total peak: 3×230M.) |
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
| **2** | `Beta` server (separate instance, port `8082`). Same code as Omega with role binding changed. Enables `row_chain[1]` from Omega. | not in this build |
| **3** | `Delta` server (separate instance, port `8083`). Enables full `omega → beta → delta` row chain in Omega's config. | not in this build |
| **4** | Rate-limiting middleware (Tower layer before `auth`). | deferred (hook reserved) |
| **5** | GitHub self-updater (`POST /api/admin/update`). | deferred (hook reserved) |
| **6** | Advanced Coding Agent with tools (extended intent classifier branch + `tool_loop.rs`). | deferred (hook reserved) |

## Deferred hooks (seams reserved in Phase 1 code)

| Feature | Where it plugs in |
| ------- | ----------------- |
| Rate limiting | Tower middleware layer in `src/main.rs` **before** `src/auth.rs`. |
| GitHub updater | `POST /api/admin/update` handler in `src/main.rs`, gated by `auth` + admin flag. |
| Advanced Coding Agent with tools | New branch in `src/orchestrator.rs` returning `Intent::Code`; `src/proxy.rs` routes to a separate `src/tool_loop.rs` module (file created later, not in Phase 1). |

## Constraints / known trade-offs

- **Quantization.** GGUF Q4_K_M for both models (faster, less precise than the archived Q8_K). One-word intent-classifier output is robust at Q4_K_M but the precision delta is observable.
- **Cold-start latency.** Every 1.2B spawn pays multi-second `llama-server` boot + 730 MB GGUF load. Accepted by spec; surfaced through `/api/public_metrics`.
- **Hard cap.** Spec ceiling is 3 concurrent 1.2B instances. The 4th concurrent caller queues, not rejects.
- **Baseline-resilience.** Baseline 230M respawn is critical-path: Omega's `8080` does not bind until baseline 230M reports `/health` ok. Spec-intent: never serve traffic if orchestrator is down.
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
