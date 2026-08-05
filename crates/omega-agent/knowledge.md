# Ashat Neural Network project knowledge

This file gives Freebuff context about your project: goals, commands, conventions, and gotchas.

## Project

**Ashat Neural Network BrainStem service** — A private inference appliance running on an Oracle Linux ARM64 server. It accepts authenticated requests, runs a persistent single CPU `llama-server` instance, collects sanitized metrics, and displays a read-only public telemetry dashboard.

Key code locations:
- `app.py` — FastAPI wiring and inference orchestration
- `config.py` — JSON-only runtime configuration loader
- `domain.py` — `Lane` enum (single `BRAINSTEM`), per-lane config, request validation
- `dashboard.py` — Server-rendered dashboard HTML
- `installer.py` — local `llama-server` availability check
- `backend_launcher.py` — persistent `llama-server` subprocess lifecycle
- `completion_client.py` — HTTP client to live llama-server backend
- `metrics_store.py` / `run_metrics.py` — sanitized metric recording
- `public_snapshot.py` — public status/metrics projection
- `server-config.example.json` — safe checked-in local configuration template
- `server-config.json` — ignored local/production configuration; never commit
- `tests/` — unittest suite

## Commands

| Action | Command |
|---|---|
| Install | `pip install -r requirements.txt` |
| Test | `python -m unittest discover tests -v` |
| Run locally | `python -m uvicorn app:app --host 127.0.0.1 --port 8000` |

## Architecture & Data Flow

1. Boot: systemd starts FastAPI after `network-online.target`.
2. `config.py` loads `/home/opc/Projects/AshatNueralHost/server-config.json` first, then local fallback paths.
3. `startup()` verifies the llama-server binary, model, and persistent CPU backend.
4. `POST /v1/chat/completions` authenticates with `X-Ashat-Key`, resolves BrainStem, validates the request, and serializes one inference at a time.
5. `BackendLauncher` starts or reuses the local llama-server process on `127.0.0.1:18080`.
6. The dashboard reads the sanitized in-memory metrics store.

## Conventions

- **JSON-only runtime configuration:** Runtime settings are loaded from `server-config.json`; production does not use an env file or environment variables.
- **Secret separation:** `server-config.example.json` contains only a placeholder. Production `server-config.json` is ignored by Git and protected as `root:opc` mode `640`.
- **Strict lane typing:** `Lane` is a closed `str, Enum` with one `BRAINSTEM` value.
- **Persistent backend:** One CPU llama-server stays alive across requests and is restarted after health failure.
- **No secrets in logs/metrics:** Prompts, responses, BrainStem keys, and tokens are never stored.
- **Uniform error envelope:** All `RunError` subclasses map to sanitized JSON errors.
- **Port layout:** FastAPI listens on `127.0.0.1:8000`; llama-server listens on `127.0.0.1:18080`; Nginx terminates public HTTPS.
- **OS startup:** `ashat-neural-host.service` is enabled in `multi-user.target`, waits for network-online, and uses `Restart=always`.

## Gotchas

- `server-config.json` is intentionally ignored; do not overwrite the production copy during source synchronization.
- The deployed local model is `LFM2.5-1.2B-Instruct-Q8_0.gguf`.
- The Oracle host is CPU-only and constrained to one thread and one concurrent inference by default.
- The native ARM64 llama-server is installed at `/usr/local/libexec/ashat-neural-host/llama-server`; the model is stored under `/var/lib/ashat-neural-host`.
- Metrics are sanitized and persisted to the configured JSONL path; no prompt or response content is persisted.
