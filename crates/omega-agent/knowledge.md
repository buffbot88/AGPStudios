# Project knowledge

This file gives Freebuff context about your project: goals, commands, conventions, and gotchas.

## Project

**AshatOS BrainStem Inference Host** — A private inference appliance running on Hugging Face Spaces (zeroGPU). It accepts authenticated requests, runs on-demand GGUF inference via `llama-server`, collects sanitized metrics, and displays a read-only public telemetry dashboard.

Key code locations:
- `app.py` — Slim orchestrator: FastAPI wiring, `@spaces.GPU` entry point, Run pipeline
- `domain.py` — `Lane` enum (single `BRAINSTEM`), per-lane config (`LANE_CONFIG`), request validation
- `dashboard.py` — Server-rendered dashboard HTML (pure FastAPI, no Gradio)
- `installer.py` / `install_strategies.py` — `llama-server` binary download + installation logic
- `backend_launcher.py` — Per-request `llama-server` subprocess lifecycle
- `completion_client.py` — HTTP client to live llama-server backend
- `lane_resolver.py` — Strict route-or-model lane routing
- `run_errors.py` — Typed exception hierarchy → JSON error codes
- `run_metrics.py` / `metrics_store.py` — Sanitized metric recording + thread-safe rolling deque
- `public_snapshot.py` — Public status/metrics projection (no prompts or keys)
- `response_adapter.py` — Response envelope shaping
- `telemetry.py` — Boot telemetry seeding
- `llama_stderr_parser.py` — Streaming parser for llama-server stderr (CUDA offload detection)
- `tests/` — unittest suite (11 test files), pure-logic tests don't need network

## Commands

| Action | Command |
|---|---|
| Install | `pip install -r requirements.txt` + `apt-get install ...` (see Dockerfile for system deps) |
| Test | `python -m unittest discover tests -v` |
| Run locally | `python app.py` (binds port 7860 via uvicorn) |
| Docker build | `docker build -t ashatos-host .` |
| Typecheck | Not configured — runtime uses `from __future__ import annotations` |
| Lint | Not configured — no formatter/linter pinned |

## Architecture & Data Flow

1. Boot: `app.py` starts, spawns daemon thread running `startup()` which:
   - Installs `llama-server` binary (GitHub releases → HF mirror fallback)
   - Pre-downloads GGUF model from HF Hub
   - Seeds telemetry with honest lane state (online/waking/degraded/offline)
2. Request arrives at `POST /v1/chat/completions` → `LaneResolver` maps model/alias → `Lane.BRAINSTEM`
3. `validate_request()` checks messages, max_tokens, temperature, top_p, body size
4. `execute_lane()` acquires `_inference_lock`, calls `@spaces.GPU`-decorated function
5. ZeroGPU worker (isolated process) runs `_run_pipeline()`:
   - `BackendLauncher.launch()` starts llama-server subprocess, waits for `/health`
   - `CompletionClient.complete()` sends HTTP POST to local llama-server
   - Returns success/failure envelope (no metrics recorded in worker!)
6. Back in main process: `_record_returned_result()` writes sanitized metrics to `METRICS` store
7. Dashboard polls `GET /api/dashboard_html` → `PublicSnapshot` renders status/card HTML

**Critical:** Metrics are recorded in the *main* process after `@spaces.GPU` returns, because ZeroGPU workers have a process-local copy of `METRICS`.

## Conventions

- **Strict lane typing:** `Lane` is a closed `str, Enum` (`BRAINSTEM = "brainstem"`). Never use free strings.
- **Per-request llama-server:** Subprocess starts per request, terminates in `finally` block. No persistent process.
- **No secrets in logs/metrics:** Prompts, responses, API keys, and paths are NEVER stored. Only sanitized aggregates.
- **Env var config:** All config read from env vars at import time via `os.getenv()`. Space Secrets override defaults.
- **Uniform error envelope:** All `RunError` subclasses → `{"ok": False, "error": {"code": ..., "message": ...}}`.
- **OpenAI-compatible API:** Uses `/v1/chat/completions` format with `messages` array, `choices[0].message.content` response.
- **Auth:** `X-Ashat-Key` header only (not `Authorization`). Constant-time HMAC comparison via `hmac.compare_digest()`.
- **No Gradio runtime:** After pivot to `sdk: docker`, the app is pure FastAPI. No `gr.*` imports.
- **Code style:** Python 3.11+, `from __future__ import annotations` everywhere, Google-style docstrings, `_` prefix for private names.

## Gotchas

- **ZeroGPU metrics isolation:** `_run_pipeline()` inside `@spaces.GPU` runs in a separate process. Never record metrics there — they'd be lost. Always record in the main process after the GPU function returns.
- **`sdk: docker` means no Gradio:** HF Spaces no longer injects Gradio runtime. All routes (including dashboard) are pure FastAPI. The Dockerfile CMD runs `uvicorn app:app --port 7860`.
- **Static `@spaces.GPU` detection:** The decorator parameters must be trivially readable by HF Spaces' AST scanner. Use plain module-level variables (e.g., `_BRAINSTEM_GPU_DURATION`), not dynamic lookups.
- **Port 7860 is mandatory:** HF Spaces proxy only forwards to container port 7860. Never change the bind port.
- **Startup runs in daemon thread:** Binary install + model download happens in a background thread so FastAPI can bind immediately. `startup()` is wrapped with `log.exception()` because Python silently swallows exceptions in daemon threads.
- **Singleton METRICS:** `METRICS` is a module-level `MetricsStore` singleton. The dashboard timer reads from it directly — no database, no file I/O.
- **Constant-time auth:** Always use `hmac.compare_digest()` for key comparison, never `==`.
- **GPU offload verification:** `LlamaServerStderrParser` parses llama-server stderr to confirm actual GPU offload. The `await_offload()` Event replaces old timing-based drains.
- **env-var override timing:** Config from env vars is read at module import time. Space Secrets changes require a restart.
- **No streaming:** `"stream": true` in a request returns an error. Not yet implemented.
