#!/usr/bin/env python3
"""AshatOS Neural I/O Host — slim orchestrator (single BrainStem lane).

The heavy lifting now lives in purpose-built modules:
    * :mod:`domain`            — Lane enum + per-lane config (single BRAINSTEM)
    * :mod:`run_errors`        — typed exception hierarchy + RunError\u2192JSON codes
    * :mod:`lane_resolver`     — strict route-or-model lane routing
    * :mod:`lane_resolver`     — strict route-or-model lane routing
    * :mod:`backend_launcher`  — per-request llama-server lifecycle
    * :mod:`completion_client` — HTTP-only client to the live backend
    * :mod:`run_metrics`       — sanitized metric + event recording
    * :mod:`metrics_store`     — thread-safe in-memory rolling deque
    * :mod:`installer`         — local llama-server availability check

What stays here: logging, configuration defaults, FastAPI wiring, the slim
Run pipeline that composes the modules above, request validation, response
envelope shaping, the atexit cleanup hook, and the homepage dashboard.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os

from config import CONFIG
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

import requests

from fastapi import FastAPI, Request as FastRequest
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from auth import is_valid_api_key
from backend_launcher import BackendLauncher, LiveBackend
from completion_client import CompletionClient, CompletionResult
from domain import LANE_CONFIG, Lane, lane_cfg
from installer import InstallerResult, ensure_llama_server

from lane_resolver import LaneResolver
from metrics_store import METRICS
from run_errors import (
    ERROR_CODE_TO_HTTP_STATUS,
    InferenceUnavailableError,
    InvalidRequestError,
    LocalModelUnavailableError,
    RunError,
)
from run_metrics import RunMetrics
from response_adapter import envelope_to_response



# ──────────────────────────────────────────────────────────────────────────
# 1.  Logging (stdout only)
# ──────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=CONFIG.log_level.upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
_log = logging.getLogger("ashatos")


# ──────────────────────────────────────────────────────────────────────────
# 2.  Configuration — runtime-only knob names
# ──────────────────────────────────────────────────────────────────────────

LLAMA_SERVER_PORT = CONFIG.llama_server_port
N_THREADS = CONFIG.n_threads
N_BATCH = CONFIG.n_batch
QUEUE_LIMIT = CONFIG.queue_limit
PUBLIC_REFRESH_SECONDS = CONFIG.public_refresh_seconds
BRAINSTEM_KEY = CONFIG.brainstem_key
# Web-only boot keeps the public dashboard available while inference assets
# are being installed. Production keeps this false in server-config.json.
ASHAT_WEB_ONLY = CONFIG.web_only


# ──────────────────────────────────────────────────────────────────────────
# 3.  Global runtime state
# ──────────────────────────────────────────────────────────────────────────

_started_at: float = time.time()
_inference_lock = threading.BoundedSemaphore(QUEUE_LIMIT)

_active_processes: list[subprocess.Popen[str]] = []
_llama_bin_path: str | None = None
_queue_depth: int = 0
_queue_depth_lock = threading.Lock()

# Per-key concurrency: maps a key-hash to a semaphore allowing up to
# ``PER_KEY_CONCURRENCY`` concurrent requests per unique key.
# A sentinel key is used for unauthenticated requests.
# This REPLACES the previous global threading.Lock — the per-key
# semaphore is the primary gate; the BoundedSemaphore above is a
# total-concurrency safety cap.
_PER_KEY_CONCURRENCY = 1
_key_semaphores: dict[str, threading.BoundedSemaphore] = {}
_key_semaphores_lock = threading.Lock()


def _get_key_semaphore(key_id: str) -> threading.BoundedSemaphore:
    """Return (or create) a bounded semaphore for the given key, allowing
    at most ``_PER_KEY_CONCURRENCY`` concurrent acquisitions.

    ``BoundedSemaphore`` prevents drift — if ``release()`` is ever called
    more times than ``acquire()``, it raises ``ValueError`` instead of
    silently allowing more concurrency than intended.
    """
    with _key_semaphores_lock:
        if key_id not in _key_semaphores:
            _key_semaphores[key_id] = threading.BoundedSemaphore(_PER_KEY_CONCURRENCY)
        return _key_semaphores[key_id]


def _get_queue_depth() -> int:
    """Return the current request queue depth (thread-safe)."""
    with _queue_depth_lock:
        return _queue_depth


def _inc_queue_depth() -> None:
    with _queue_depth_lock:
        global _queue_depth
        _queue_depth += 1


def _dec_queue_depth() -> None:
    with _queue_depth_lock:
        global _queue_depth
        if _queue_depth > 0:
            _queue_depth -= 1


def _binary_path_getter() -> str | None:
    return _llama_bin_path


# Pipeline collaborators instantiated once at module import.
_RESOLVER = LaneResolver()
_BACKEND_LAUNCHER = BackendLauncher(
    binary_path_getter=_binary_path_getter,
    port=LLAMA_SERVER_PORT,
    n_threads=N_THREADS,
    n_batch=N_BATCH,
)
_COMPLETION_CLIENT = CompletionClient(default_timeout_s=120.0)
_RUN_METRICS = RunMetrics(METRICS)


# ──────────────────────────────────────────────────────────────────────────
# 4.  atexit cleanup
# ──────────────────────────────────────────────────────────────────────────

def _terminate_process(proc: subprocess.Popen[str] | None, name: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    except Exception:
        pass


def stop_all_servers() -> None:
    _BACKEND_LAUNCHER.stop()
    for proc in list(_active_processes):
        _terminate_process(proc, "atexit")


atexit.register(stop_all_servers)


# ──────────────────────────────────────────────────────────────────────────
# 5.  Request validation (delegates to domain)
# ──────────────────────────────────────────────────────────────────────────

from domain import validate_request


# ──────────────────────────────────────────────────────────────────────────
# 6.  Run pipeline — the slim orchestrator
# ──────────────────────────────────────────────────────────────────────────

def _is_cold_start(lane: Lane) -> bool:
    """Returns True the first time this lane is asked to run."""
    return not LANE_CONFIG[lane].model_path or not os.path.isfile(
        LANE_CONFIG[lane].model_path
    )


def _build_success_envelope(
    lane: Lane,
    request_id: str,
    backend: LiveBackend,
    completion: CompletionResult,
    total_ms: float,
    cold_start: bool,
) -> dict[str, Any]:
    """Shape the public response envelope (OpenAI-compatible + ashat extras)."""
    cfg = lane_cfg(lane)
    prompt_tokens = completion.prompt_tokens or 0
    completion_tokens = completion.completion_tokens or 0
    total_tokens = (
        completion.total_tokens
        if completion.total_tokens is not None
        else prompt_tokens + completion_tokens
    )
    return {
        "id": f"ashat-{request_id[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": cfg.file,
        "lane": lane.value,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": completion.text},
                "finish_reason": completion.finish_reason or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        "performance": {
            "cold_start": cold_start,
            "server_start_ms": backend.server_start_ms,
            "model_load_ms": backend.model_load_ms or 0.0,
            "total_latency_ms": total_ms,
            "time_to_first_token_ms": completion.time_to_first_token_ms,
            "prompt_tokens_per_second": completion.prompt_tokens_per_second or 0.0,
            "generation_tokens_per_second": completion.generation_tokens_per_second or 0.0,
            "backend": backend.backend_mode,
        },
        "request_id": request_id,
        "ok": True,
    }


def _build_failure_envelope(
    lane: Lane | None,
    request_id: str,
    exc: RunError,
) -> dict[str, Any]:
    return {
        "ok": False,
        "request_id": request_id,
        "lane": lane.value if lane else "unknown",
        "error": exc.to_envelope(),
    }


def _run_pipeline(lane: Lane, payload: dict[str, Any]) -> dict[str, Any]:
    """Slim Run. Composes :class:`BackendLauncher`, :class:`CompletionClient`
    into one request lifecycle.

    Behavior:
      * Degraded-mode gate first — INFERENCE_UNAVAILABLE without spawning a
        subprocess with an empty binary path.
      * Typed :class:`RunError` subclasses never bubble up; the orchestrator
        converts them to a uniform failure envelope.
      * ``BackendLauncher`` and ``CompletionClient`` are responsible for all
        subprocess / HTTP edges; this function is orchestration only.
      * The outermost ``except Exception`` is the only broad catch — it's
        the safety boundary.
    """
    request_id = str(payload.get("request_id") or uuid.uuid4())
    payload.setdefault("request_id", request_id)

    started_at = time.perf_counter()
    cold_start = _is_cold_start(lane)

    # Degraded-mode gate.
    if not _llama_bin_path:
        _log.warning(
            "%s: inference unavailable \u2014 llama-server binary not installed",
            lane.value,
        )
        exc = InferenceUnavailableError(
            "llama-server binary not installed (degraded mode)"
        )
        return _build_failure_envelope(lane, request_id, exc)

    # Oracle is CPU-only; keep one persistent llama-server process alive.
    try:
        backend = _BACKEND_LAUNCHER.ensure_started(
            lane, gpu_offload_requested=False,
        )
        try:
            completion = _COMPLETION_CLIENT.complete(backend, lane, payload)
        except RunError:
            _BACKEND_LAUNCHER.invalidate()
            raise
        total_ms = round((time.perf_counter() - started_at) * 1000, 1)
        return _build_success_envelope(
            lane, request_id, backend, completion, total_ms, cold_start,
        )

    except RunError as exc:
        return _build_failure_envelope(lane, request_id, exc)

    except Exception as exc:
        # Outermost safety boundary. Never let a stray runtime error kill
        # the request silently.
        _log.exception("%s: unhandled exception in run pipeline", lane.value)
        envelope = {
            "code": "INTERNAL_ERROR",
            "message": str(exc)[:200],
            "retryable": True,
        }
        return {
            "ok": False, "request_id": request_id, "lane": lane.value,
            "error": envelope,
        }


# ──────────────────────────────────────────────────────────────────────────
# 7.  Single BrainStem execution entry point
# ──────────────────────────────────────────────────────────────────────────

def _execute_brainstem(payload: dict[str, Any]) -> dict[str, Any]:
    return _run_pipeline(Lane.BRAINSTEM, payload)


# ──────────────────────────────────────────────────────────────────────────
# 7b.  Metric recording
# ──────────────────────────────────────────────────────────────────────────

def _record_returned_result(
    lane: Lane,
    result: dict[str, Any],
) -> None:
    """
    Record a sanitized local inference result in the dashboard process.

    Delegates to :meth:`RunMetrics.record_from_envelope` to eliminate
    the duplicate ``MetricRecord`` construction that previously existed
    here and in :meth:`RunMetrics.record_success`.

    Called by :func:`execute_lane` after the inference function returns.
    Never stores prompts, generated text, request IDs, keys, or headers —
    only sanitized aggregates.
    """
    _RUN_METRICS.record_from_envelope(lane, result)


def execute_lane(lane_str: str, payload: dict[str, Any], *,
                 key_id: str | None = None) -> dict[str, Any]:
    """Serializing entry point \u2014 one inference at a time on the host.

    Metrics are recorded after the local inference result returns so the
    dashboard reads them from the same in-memory ``METRICS`` singleton.

    Tracks queue depth via :func:`_inc_queue_depth` / :func:`_dec_queue_depth`
    so the dashboard can display how many requests are waiting.

    Per-key concurrency: when ``key_id`` is provided, a key-specific
    semaphore allows ``PER_KEY_CONCURRENCY`` requests to run in parallel
    for that key.  Without a key, the global ``_inference_lock`` is used.
    """
    lane = Lane.parse(lane_str)
    sem = _get_key_semaphore(key_id or "_anonymous_")
    _inc_queue_depth()
    try:
        # Per-key semaphore is the primary concurrency gate (2 per key).
        # The BoundedSemaphore (QUEUE_LIMIT) is the total-cap safety net.
        with sem:
            with _inference_lock:
                result = _execute_brainstem(payload)
    finally:
        _dec_queue_depth()

    _record_returned_result(lane, result)
    return result


# ──────────────────────────────────────────────────────────────────────────
# 8.  HTTP surface adapter
# ──────────────────────────────────────────────────────────────────────────

def _envelope_to_response(envelope: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Backwards-compat shim \u2014 see :func:`response_adapter.envelope_to_response`."""
    return envelope_to_response(envelope)


# ── FastAPI adapter ─────────────────────────────────────────────────────

def _make_http_chat_completions():
    """Factory for :func:`http_chat_completions` so it lazily resolves per request."""
    resolver = _RESOLVER

    async def http_chat_completions(request: FastRequest) -> JSONResponse:
        # 1. Authenticate before parsing or queueing inference.
        supplied = request.headers.get("X-Ashat-Key", "")
        if not is_valid_api_key(supplied, BRAINSTEM_KEY):
            return JSONResponse(status_code=401, content={
                "error": {
                    "message": "Missing or invalid X-Ashat-Key",
                    "type": "authentication_error",
                },
            })

        # 2. Parse JSON.
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={
                "error": {"message": "Invalid JSON body", "type": "invalid_request_error"},
            })

        # 3. Lane resolution (single BrainStem lane)
        try:
            lane = resolver.resolve(body, route_hint=None)
        except InvalidRequestError as exc:
            status = ERROR_CODE_TO_HTTP_STATUS.get(exc.code, 400)
            return JSONResponse(status_code=status, content={
                "error": {"message": exc.message, "type": exc.code.lower()},
            })

        # 4. Validate
        try:
            err = validate_request(body, lane)
            if err:
                raise InvalidRequestError(err)
        except InvalidRequestError as exc:
            return JSONResponse(status_code=400, content={
                "error": {"message": exc.message, "type": exc.code.lower()},
            })

        # 5. Run pipeline in executor (avoid blocking the event loop)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, execute_lane, lane.value, body)

        # 6. Response envelope
        status, payload = _envelope_to_response(result)
        return JSONResponse(status_code=status, content=payload)

    return http_chat_completions


# ──────────────────────────────────────────────────────────────────────────
# 9.  Public status / metrics / dashboard HTML
#     All three public surfaces funnel through PublicSnapshot — one
#     projection, one redaction pass, three HTML/JSON consumers.
# ──────────────────────────────────────────────────────────────────────────

from public_snapshot import PublicSnapshot, RuntimeState
from telemetry import TELEMETRY
from dashboard import render_index_html, render_dashboard_html_json


def _snapshot() -> PublicSnapshot:
    """Build a fresh snapshot from current runtime state. Cheap (no I/O)."""
    return PublicSnapshot.from_metrics(
        METRICS,
        RuntimeState(
            started_at=_started_at,
            llama_server_available=_llama_bin_path is not None,
            llama_server_path=_llama_bin_path,
            queue_depth=_get_queue_depth(),
            queue_limit=QUEUE_LIMIT,
        ),
        LANE_CONFIG,
    )


# Backwards-compat shim for any caller that used the old name:
def _build_status() -> dict[str, Any]:
    return _snapshot().render_status()


def _public_status_json() -> str:
    return json.dumps(_snapshot().render_status())


def _public_metrics_json() -> str:
    return json.dumps(_snapshot().render_metrics())


def _status_html() -> str:
    return _snapshot().render_html()


# ──────────────────────────────────────────────────────────────────────────
# 10.  FastAPI routes
# ──────────────────────────────────────────────────────────────────────────

_BINARY_FAILURE_EXC: dict[str, type[RunError]] = {}


def startup() -> None:
    """Boot sequence — verify local runtime assets and seed telemetry.

    The seed telemetry state is honest about reality: a broken boot never
    claims ``lane_state="online"``.

    """
    global _llama_bin_path
    _log.info("=" * 60)
    _log.info("AshatOS Neural I/O Host \u2014 Single-Lane BrainStem Inference")
    if ASHAT_WEB_ONLY:
        _log.info("Web-only mode enabled; inference startup is deferred")
        TELEMETRY.seed_boot(
            Lane.BRAINSTEM,
            backend="cpu",
            lane_state="offline",
            host_state="online",
        )
        METRICS.add_event(
            "brainstem: web-only mode active (inference startup deferred)"
        )
        return
    _log.info("=" * 60)

    # Pass 1: llama-server binary.
    bin_result: InstallerResult = ensure_llama_server()
    _llama_bin_path = bin_result.path
    if _llama_bin_path:
        _log.info("llama-server binary: %s", _llama_bin_path)
    else:
        _log.warning(
            "llama-server binary not available (code=%s msg=%s) \u2014 degraded mode",
            bin_result.failure_code or "BINARY_INSTALL_FAILED",
            bin_result.failure_message or "(no detail)",
        )
        # Record binary-install failures against the lane so the
        # dashboard surfaces them as a clear "BINARY MISSING" pill.
        if bin_result.failure_code:
            exc_cls = _BINARY_FAILURE_EXC.get(
                bin_result.failure_code, InferenceUnavailableError,
            )
            err = exc_cls(
                bin_result.failure_message or "llama-server binary install failed",
            )
            for lane in (Lane.BRAINSTEM,):
                _RUN_METRICS.record_failure(
                    lane,
                    request_id="startup-binary",
                    error=err,
                    elapsed_ms=0.0,
                    cold_start=True,
                )

    # Pass 2: local model verification.
    model_failure_code: str | None = None
    model_ready = False
    if _llama_bin_path:
        for lane in (Lane.BRAINSTEM,):
            try:
                path = _BACKEND_LAUNCHER.ensure_model(lane)
                _log.info(
                    "%s local model verified: %s", lane.value, path,
                )
                _BACKEND_LAUNCHER.ensure_started(
                    lane, gpu_offload_requested=False,
                )
                model_ready = True
            except LocalModelUnavailableError as exc:
                model_failure_code = "LOCAL_MODEL_UNAVAILABLE"
                _log.warning(
                    "%s local model verification failed: %s", lane.value, exc,
                )
                _RUN_METRICS.record_failure(
                    lane, request_id="startup-model",
                    error=exc, elapsed_ms=0.0, cold_start=True,
                )
            except Exception as exc:
                model_failure_code = "LOCAL_MODEL_UNAVAILABLE"
                _log.warning(
                    "%s local model verification failed (unknown): %s: %s",
                    lane.value, type(exc).__name__, exc,
                )
                _RUN_METRICS.record_failure(
                    lane, request_id="startup-model",
                    error=LocalModelUnavailableError(
                        f"{lane.value}: local model verification raised "
                        f"{type(exc).__name__}: {exc}",
                    ),
                    elapsed_ms=0.0,
                    cold_start=True,
                )

    # Pass 3: seed boot telemetry with HONEST state for each lane.
    for lane in (Lane.BRAINSTEM,):
        if model_ready and _llama_bin_path:
            TELEMETRY.seed_boot(lane, backend="cpu")
        elif not _llama_bin_path:
            TELEMETRY.seed_boot(
                lane, backend="cpu",
                lane_state="offline", host_state="offline",
            )
        else:
            TELEMETRY.seed_boot(
                lane, backend="cpu",
                lane_state="waking", host_state="starting",
            )

        # Always emit a single, explicit startup event so the operator
        # can see exactly what happened even when seed_boot claims a
        # degraded state \u2014 the event log is the most reliable surface.
        METRICS.add_event(
            f"{lane.value}: startup complete "
            f"(binary={'ready' if _llama_bin_path else 'missing'}, "
            f"model={'ready' if model_ready else model_failure_code or 'missing'})"
        )


# Run startup() in a daemon thread so FastAPI binds immediately while the
# local runtime verification completes. The dashboard shows a waking state
# until startup finishes. Any unhandled startup exception is logged instead
# of being silently swallowed by the daemon thread.
def _run_startup_with_logging() -> None:
    try:
        startup()
    except Exception:
        _log.exception(
            "startup daemon thread crashed; host will run degraded (binary "
            "or model may be unreachable). Check the service journal."
        )

_startup_thread = threading.Thread(
    target=_run_startup_with_logging, daemon=True, name="ashatos-startup",
)
_startup_thread.start()


# ──────────────────────────────────────────────────────────────────────────
# 12.  FastAPI serving. The dashboard is rendered server-side and refreshed
#      by a small browser polling loop.
# ──────────────────────────────────────────────────────────────────────────


# Hoist the chat-completions inner async handler to module scope so
# every request reuses the same closure rather than rebuilding one
# via `_make_http_chat_completions()(request)` on every request.
_chat_completions_handler = _make_http_chat_completions()


app = FastAPI(title="AshatOS Neural Host")

# Serve images directory for dashboard logos (use absolute path)
_IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
app.mount("/images", StaticFiles(directory=_IMAGES_DIR), name="images")


@app.get("/api/public_status")
async def http_public_status() -> JSONResponse:
    return JSONResponse(content=_build_status())


@app.get("/api/public_metrics")
async def http_public_metrics() -> JSONResponse:
    return JSONResponse(content=_snapshot().render_metrics())


@app.get("/api/dashboard_html")
async def http_dashboard_html() -> JSONResponse:
    """Live-refresh companion to GET /; client JS polls this endpoint.

    Returns server-rendered status-row + brainstem-card HTML
    snippets. The browser script in render_index_html polls this
    endpoint and innerHTML-swaps the corresponding divs. Styling
    logic stays in ONE place (dashboard.py) rather than being
    duplicated in client JavaScript.
    """
    snap = _snapshot()
    return JSONResponse(content=render_dashboard_html_json(snap))


@app.get("/api/dashboard_timeseries")
async def http_dashboard_timeseries() -> JSONResponse:
    """Plotly time-series companion. Returns per-lane frame data for
    client-side Plotly chart rendering.

    Each frame entry: timestamp, generation_tokens_per_second,
    total_latency_ms, time_to_first_token_ms, prompt_tokens_per_second,
    and success flag.  Safe for public consumption — no prompts, keys,
    or paths.
    """
    frames = _snapshot().render_frames()
    return JSONResponse(content=frames)


@app.get("/health")
async def http_health() -> JSONResponse:
    backend_healthy = False
    try:
        backend_healthy = requests.get(
            f"http://127.0.0.1:{LLAMA_SERVER_PORT}/health", timeout=2,
        ).status_code == 200
    except Exception:
        backend_healthy = False
    return JSONResponse(content={
        "status": "ok",
        "uptime_seconds": round(time.time() - _started_at, 1),
        "brainstem_ready": bool(
            LANE_CONFIG[Lane.BRAINSTEM].model_path
            and os.path.isfile(LANE_CONFIG[Lane.BRAINSTEM].model_path)
        ),
        "llama_server_available": backend_healthy,
    })


@app.get("/v1/models")
async def http_list_models() -> JSONResponse:
    return JSONResponse(content={
        "object": "list",
        "data": [
            {
                "id": lane_cfg(Lane.BRAINSTEM).file,
                "object": "model",
                "created": int(_started_at),
                "owned_by": "ashatos",
            },
        ],
    })


@app.post("/v1/chat/completions")
async def http_chat_completions(request: FastRequest) -> JSONResponse:
    return await _chat_completions_handler(request)


@app.get("/", response_class=HTMLResponse)
async def http_landing() -> HTMLResponse:
    """Public telemetry dashboard with periodic browser refresh."""
    return HTMLResponse(
        content=render_index_html(
            snapshot_provider=_snapshot,
            refresh_seconds=PUBLIC_REFRESH_SECONDS,
        )
    )





if __name__ == "__main__":
    # Local development entry point. Production uses the systemd unit.

    import uvicorn

    _target_port = 8000
    _log.info(
        "Boot: uvicorn.run(app, host=0.0.0.0, port=%d)",
        _target_port,
    )
    print(f"Running on local URL:  http://0.0.0.0:{_target_port}")
    uvicorn.run(app, host="0.0.0.0", port=_target_port)
