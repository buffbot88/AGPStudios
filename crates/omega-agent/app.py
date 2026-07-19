#!/usr/bin/env python3
"""AshatOS Neural I/O Host — slim orchestrator (single BrainStem lane).

The heavy lifting now lives in purpose-built modules:
    * :mod:`domain`            — Lane enum + per-lane config (single BRAINSTEM)
    * :mod:`run_errors`        — typed exception hierarchy + RunError\u2192JSON codes
    * :mod:`lane_resolver`     — strict route-or-model lane routing
    * :mod:`lane_keygate`      — single auth authority for both surfaces
    * :mod:`backend_launcher`  — per-request llama-server lifecycle
    * :mod:`completion_client` — HTTP-only client to the live backend
    * :mod:`run_metrics`       — sanitized metric + event recording
    * :mod:`metrics_store`     — thread-safe in-memory rolling deque
    * :mod:`installer`         — bin installer + GitHub/HF mirror tiers

What stays here: logging, configuration defaults, the FastAPI and Gradio
wiring, the slim Run pipeline that composes the modules above, request
validation, response envelope shaping, the atexit cleanup hook, and the
homepage dashboard.

Both the Gradio API lane endpoints and the FastAPI OpenAI-compatible route
funnel into a single :func:`_run_pipeline`. There is no copy-paste in
between; surface differences are encapsulated in two thin adapters that end
up calling the same orchestrator.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request as FastRequest
from fastapi.responses import HTMLResponse, JSONResponse

from backend_launcher import BackendLauncher, LiveBackend
from completion_client import CompletionClient, CompletionResult
from domain import LANE_CONFIG, Lane, lane_cfg
from installer import InstallerResult, ensure_llama_server
from lane_keygate import (
    AuthError,
    LaneKeyGate,
    headers_from_fastapi,
    headers_from_gradio,
)
from lane_resolver import LaneResolver
from metrics_store import METRICS, MetricRecord
from run_errors import (
    ERROR_CODE_TO_HTTP_STATUS,
    HfCreditsExhaustedError,
    HfRateLimitedError,
    InferenceUnavailableError,
    InvalidRequestError,
    ModelDownloadError,
    RunError,
)
from run_metrics import RunMetrics
from response_adapter import envelope_to_response

# ZeroGPU compatibility — direct @spaces.GPU decorator (needed for static detection)
try:
    import spaces
except ImportError:
    import types as _types
    spaces = _types.ModuleType("spaces")
    class _GPU:
        def __call__(self, fn=None, **kwargs):
            if fn is not None:
                return fn
            return lambda f: f
    spaces.GPU = _GPU()  # type: ignore[assignment]


# ──────────────────────────────────────────────────────────────────────────
# 1.  Logging (stdout only)
# ──────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
_log = logging.getLogger("ashatos")


# ──────────────────────────────────────────────────────────────────────────
# 2.  Configuration (env-overridable) — runtime-only knob names
# ──────────────────────────────────────────────────────────────────────────

LLAMA_SERVER_PORT = int(os.getenv("LLAMA_SERVER_PORT", "18080"))
N_THREADS = int(os.getenv("N_THREADS", "2"))
N_BATCH = int(os.getenv("N_BATCH", "128"))
QUEUE_LIMIT = int(os.getenv("QUEUE_LIMIT", "16"))
PUBLIC_REFRESH_SECONDS = int(os.getenv("PUBLIC_REFRESH_SECONDS", "10"))
# GPU slot duration for ZeroGPU. Read once at import time and exposed
# as a plain module-level Name so the @spaces.GPU decorator below is
# trivially AST-readable by HF Spaces' static scanner.
_BRAINSTEM_GPU_DURATION = int(os.getenv("BRAINSTEM_GPU_DURATION", "120"))


# ──────────────────────────────────────────────────────────────────────────
# 3.  Global runtime state
# ──────────────────────────────────────────────────────────────────────────

_started_at: float = time.time()
_inference_lock = threading.Lock()
_active_processes: list[subprocess.Popen[str]] = []
_llama_bin_path: str | None = None


def _binary_path_getter() -> str | None:
    return _llama_bin_path


# Pipeline collaborators instantiated once at module import.
_RESOLVER = LaneResolver()
_KEY_GATE = LaneKeyGate()
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
    for proc in list(_active_processes):
        _terminate_process(proc, "atexit")


atexit.register(stop_all_servers)


# ──────────────────────────────────────────────────────────────────────────
# 5.  Request validation (delegates to domain)
# ──────────────────────────────────────────────────────────────────────────

from domain import validate_request


# ──────────────────────────────────────────────────────────────────────────
# 6.  Run pipeline — the slim orchestrator
#     NOTE: This function runs inside the ZeroGPU worker process when
#     called via @spaces.GPU. It must NOT record metrics — those writes
#     would be lost because the worker's in-memory METRICS singleton is
#     process-local.  Metrics are recorded in the main process by
#     :func:`_record_returned_result` after the GPU function returns.
# ──────────────────────────────────────────────────────────────────────────

def _is_cold_start(lane: Lane) -> bool:
    """Returns True the first time this lane is asked to run."""
    return not LANE_CONFIG[lane]["model_path"] or not os.path.isfile(
        LANE_CONFIG[lane]["model_path"]
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
        "model": cfg["file"],
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
            "gpu_offload_verified": backend.gpu_offload_verified,
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

    .. caution::

       This function is called from inside ``@spaces.GPU`` which may run
       in an isolated worker process.  **It must not record metrics** —
       those writes would land in a process-local copy of ``METRICS``
       that the main Gradio process never sees.  Metrics are recorded in
       the main process by :func:`_record_returned_result` after the GPU
       function returns.

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

    # On ZeroGPU, CUDA is managed by the spaces package. The llama-server
    # subprocess must not request GPU offload (the -ngl flag).
    _is_zerogpu = bool(int(os.environ.get("SPACES_ZERO_GPU", "0")))
    try:
        with _BACKEND_LAUNCHER.launch(
            lane, gpu_offload_requested=not _is_zerogpu,
        ) as backend:
            _active_processes.append(backend.process)
            try:
                completion = _COMPLETION_CLIENT.complete(backend, lane, payload)
            finally:
                try:
                    _active_processes.remove(backend.process)
                except ValueError:
                    pass
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
# 7.  @spaces.GPU wrapper — one entry for the single BrainStem lane
# ──────────────────────────────────────────────────────────────────────────

@spaces.GPU
def _execute_brainstem_gpu(payload: dict[str, Any]) -> dict[str, Any]:
    return _run_pipeline(Lane.BRAINSTEM, payload)


# ──────────────────────────────────────────────────────────────────────────
# 7b.  Metric recording in the main process
#      The function above may run in a ZeroGPU worker (separate process).
#      We record metrics HERE, after the result crosses back to the main
#      Gradio/FastAPI process, so the dashboard timer can read them.
# ──────────────────────────────────────────────────────────────────────────

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed >= 0 else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else default
    except (TypeError, ValueError):
        return default


def _record_returned_result(
    lane: Lane,
    result: dict[str, Any],
) -> None:
    """
    Record a sanitized ZeroGPU result in the main dashboard process.

    Called by :func:`execute_lane` after the ``@spaces.GPU`` function
    returns.  Never stores prompts, generated text, request IDs, keys,
    or headers — only sanitized aggregates.
    """
    if not isinstance(result, dict):
        METRICS.record(
            MetricRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                lane=lane.value,
                success=False,
                error_category="INVALID_MODEL_RESPONSE",
            )
        )
        METRICS.add_event(f"{lane.value}: INVALID_MODEL_RESPONSE")
        return

    ok = bool(result.get("ok"))
    performance = result.get("performance") or {}
    usage = result.get("usage") or {}
    error = result.get("error") or {}

    if not isinstance(performance, dict):
        performance = {}
    if not isinstance(usage, dict):
        usage = {}
    if not isinstance(error, dict):
        error = {}

    ttft_raw = performance.get("time_to_first_token_ms")
    # Treat zero or negative TTFT as unmeasured (same as None).
    ttft_parsed = _safe_float(ttft_raw) if ttft_raw is not None else None

    rec = MetricRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        lane=lane.value,
        success=ok,
        cold_start=bool(performance.get("cold_start", False)),
        server_start_ms=_safe_float(performance.get("server_start_ms")),
        model_load_ms=_safe_float(performance.get("model_load_ms")),
        prompt_tokens=_safe_int(usage.get("prompt_tokens")),
        completion_tokens=_safe_int(usage.get("completion_tokens")),
        prompt_tokens_per_second=_safe_float(
            performance.get("prompt_tokens_per_second")
        ),
        generation_tokens_per_second=_safe_float(
            performance.get("generation_tokens_per_second")
        ),
        time_to_first_token_ms=(
            ttft_parsed if (ttft_parsed is not None and ttft_parsed > 0) else None
        ),
        total_latency_ms=_safe_float(performance.get("total_latency_ms")),
        backend=str(performance.get("backend", "unknown")),
        gpu_offload_verified=bool(
            performance.get("gpu_offload_verified", False)
        ),
        finish_reason=(
            str(result.get("choices", [{}])[0].get("finish_reason", "stop"))
            if ok
            else ""
        ),
        error_category=(
            None if ok else str(error.get("code", "INFERENCE_FAILED"))
        ),
    )

    METRICS.record(rec)

    if ok:
        METRICS.add_event(
            f"{lane.value}: inference completed "
            f"({rec.prompt_tokens}+{rec.completion_tokens} tokens)"
        )
    else:
        METRICS.add_event(f"{lane.value}: {rec.error_category}")


def execute_lane(lane_str: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Serializing entry point \u2014 one inference at a time across the Space.

    The ``@spaces.GPU`` functions run in an isolated worker process (on
    HF Spaces with ZeroGPU).  We record metrics here, in the *main*
    process, after the result returns, so the dashboard timer can read
    them from the same in-memory ``METRICS`` singleton.
    """
    lane = Lane.parse(lane_str)
    with _inference_lock:
        result = _execute_brainstem_gpu(payload)

    _record_returned_result(lane, result)
    return result


# ──────────────────────────────────────────────────────────────────────────
# 8.  Surface adapters — fastapi (HTTP) and gradio (queue API)
# ──────────────────────────────────────────────────────────────────────────

def _envelope_to_response(envelope: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Backwards-compat shim \u2014 see :func:`response_adapter.envelope_to_response`."""
    return envelope_to_response(envelope)


# ── FastAPI adapter ─────────────────────────────────────────────────────

def _make_http_chat_completions():
    """Factory for :func:`http_chat_completions` so it lazily resolves per request."""
    resolver = _RESOLVER

    async def http_chat_completions(request: FastRequest) -> JSONResponse:
        # 1. Parse JSON
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={
                "error": {"message": "Invalid JSON body", "type": "invalid_request_error"},
            })

        # 2. Lane resolution (single BrainStem lane)
        try:
            lane = resolver.resolve(body, route_hint=None)
        except InvalidRequestError as exc:
            status = ERROR_CODE_TO_HTTP_STATUS.get(exc.code, 400)
            return JSONResponse(status_code=status, content={
                "error": {"message": exc.message, "type": exc.code.lower()},
            })

        # 3. Auth
        try:
            _KEY_GATE.check(headers_from_fastapi(request), lane)
        except AuthError:
            return JSONResponse(status_code=401, content={
                "error": {"message": "unauthorized", "type": "authentication_error"},
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


def _snapshot() -> PublicSnapshot:
    """Build a fresh snapshot from current runtime state. Cheap (no I/O)."""
    return PublicSnapshot.from_metrics(
        METRICS,
        RuntimeState(
            started_at=_started_at,
            llama_server_available=_llama_bin_path is not None,
            llama_server_path=_llama_bin_path,
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
# 10.  Pure-FastAPI routes — registered directly on `_app` at the
#      bottom of the file. The previous Gradio-mounted FastAPI +
#      `_hf_register_routes` + `_LOCAL_FASTAPI` plumbing is gone
#      now that we are on `sdk: docker` with no Gradio runtime.
# ──────────────────────────────────────────────────────────────────────────

_BINARY_FAILURE_EXC: dict[str, type[RunError]] = {
    "HF_CREDITS_EXHAUSTED": HfCreditsExhaustedError,
    "HF_RATE_LIMITED": HfRateLimitedError,
}


def startup() -> None:
    """Boot sequence — install binary, pre-fetch model, seed telemetry.

    Surfaces HF-specific failures (credits exhausted / rate limited /
    model missing) into the metrics store and dashboard via typed error
    codes rather than generic ``Exception`` silently. The seed telemetry
    state is HONEST about reality \u2014 a broken boot never claims
    ``lane_state="online"``.
    """
    global _llama_bin_path
    _log.info("=" * 60)
    _log.info("AshatOS Neural I/O Host \u2014 Single-Lane BrainStem Inference")
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

    # Pass 2: model pre-download.
    model_failure_code: str | None = None
    model_ready = False
    if _llama_bin_path:
        for lane in (Lane.BRAINSTEM,):
            try:
                path = _BACKEND_LAUNCHER.ensure_model(lane)
                _log.info(
                    "%s model cached: %s", lane.value, path,
                )
                model_ready = True
            except HfCreditsExhaustedError as exc:
                model_failure_code = "HF_CREDITS_EXHAUSTED"
                _log.error(
                    "%s model download: %s \u2014 %s",
                    lane.value, exc.code, exc.message[:200],
                )
                _RUN_METRICS.record_failure(
                    lane, request_id="startup-model",
                    error=exc, elapsed_ms=0.0, cold_start=True,
                )
            except HfRateLimitedError as exc:
                model_failure_code = "HF_RATE_LIMITED"
                _log.error(
                    "%s model download: %s \u2014 %s",
                    lane.value, exc.code, exc.message[:200],
                )
                _RUN_METRICS.record_failure(
                    lane, request_id="startup-model",
                    error=exc, elapsed_ms=0.0, cold_start=True,
                )
            except ModelDownloadError as exc:
                model_failure_code = "MODEL_DOWNLOAD_FAILED"
                _log.warning(
                    "%s model pre-download failed: %s", lane.value, exc,
                )
                _RUN_METRICS.record_failure(
                    lane, request_id="startup-model",
                    error=exc, elapsed_ms=0.0, cold_start=True,
                )
            except Exception as exc:
                model_failure_code = "MODEL_DOWNLOAD_FAILED"
                _log.warning(
                    "%s model pre-download failed (unknown): %s: %s",
                    lane.value, type(exc).__name__, exc,
                )
                _RUN_METRICS.record_failure(
                    lane, request_id="startup-model",
                    error=ModelDownloadError(
                        f"{lane.value}: pre-download raised "
                        f"{type(exc).__name__}: {exc}",
                    ),
                    elapsed_ms=0.0,
                    cold_start=True,
                )

    # Pass 3: seed boot telemetry with HONEST state for each lane.
    for lane in (Lane.BRAINSTEM,):
        if model_ready and _llama_bin_path:
            TELEMETRY.seed_boot(lane, backend="cuda", gpu_offload=True)
        elif not _llama_bin_path:
            TELEMETRY.seed_boot(
                lane, backend="cpu", gpu_offload=False,
                lane_state="offline", host_state="offline",
            )
        elif model_failure_code == "HF_CREDITS_EXHAUSTED":
            # Persistent failure (waiting on human action) \u2014 surface it
            # prominently as a degraded lane (not transient "waking").
            TELEMETRY.seed_boot(
                lane, backend="cpu", gpu_offload=False,
                lane_state="degraded", host_state="degraded",
            )
        elif model_failure_code == "HF_RATE_LIMITED":
            TELEMETRY.seed_boot(
                lane, backend="cpu", gpu_offload=False,
                lane_state="waking", host_state="starting",
            )
        else:
            TELEMETRY.seed_boot(
                lane, backend="cpu", gpu_offload=False,
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


# Run startup() in a daemon thread so the FastAPI app can bind port 7860
# IMMEDIATELY and start serving /health, /v1/models, /api/public_status, etc.
# Instead of blocking the module-level import for the duration of the binary
# install + model download (which can take 60s+ on cold cache and was the live
# Space's "Starting..." symptom for the past several commits). The dashboard
# reads `_llama_bin_path` as it gets populated; the lanes show `waking` until
# startup completes, then flip to `online` automatically. Daemon=True so the
# thread never blocks container exit if HF sends SIGTERM during shutdown.
#
# CRITICAL: any unhandled exception raised inside startup() would be swallowed
# by Python's threading layer (no traceback on the console, the daemon just
# dies). We wrap the target with a `_log.exception(...)` chokepoint so a
# crash surfaces in HF Spaces' logs tab rather than leaving the operator
# debugging a silent hang.
def _run_startup_with_logging() -> None:
    try:
        startup()
    except Exception:
        _log.exception(
            "startup daemon thread crashed; Space will run degraded (binary "
            "or model may be unreachable). Check HF Spaces logs for the cause."
        )

_startup_thread = threading.Thread(
    target=_run_startup_with_logging, daemon=True, name="ashatos-startup",
)
_startup_thread.start()

# ── Sync startup report (lets ZeroGPU platform confirm readiness) ────
try:
    from spaces.config import Config as _SC
    if _SC.zero_gpu:
        from spaces.zero import client as _zclient
        _zclient.startup_report()
        _log.info("startup_report sent")
except Exception as exc:
    _log.warning("startup_report failed: %s", exc)


# ──────────────────────────────────────────────────────────────────────────
# -------------------------------------------------------------------------
# 12.  Pure-FastAPI serving. The dashboard is rendered server-side as a
#      complete <!DOCTYPE html> document at GET /; live updates come
#      from a small JS setInterval polling /api/dashboard_html and
#      swapping the innerHTML of the status + brainstem card divs in
#      place. This mirrors the previous Gradio `gr.Timer` behavior but
#      lives in plain FastAPI + browser fetch -- NO Gradio runtime at
#      all, NO auth shim hazard, NO monkeypatch. With `sdk: docker`
#      HF Spaces runs `uvicorn app:app --host 0.0.0.0 --port 7860`
#      directly against this FastAPI.
# -------------------------------------------------------------------------


# Hoist the chat-completions inner async handler to module scope so
# every request reuses the same closure rather than rebuilding one
# via `_make_http_chat_completions()(request)` on every request.
_chat_completions_handler = _make_http_chat_completions()


_app = FastAPI(title="AshatOS Neural Host")


@_app.get("/api/public_status")
async def http_public_status() -> JSONResponse:
    return JSONResponse(content=_build_status())


@_app.get("/api/public_metrics")
async def http_public_metrics() -> JSONResponse:
    return JSONResponse(content=_snapshot().render_metrics())


@_app.get("/api/dashboard_html")
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


@_app.get("/health")
async def http_health() -> JSONResponse:
    return JSONResponse(content={
        "status": "ok",
        "uptime_seconds": round(time.time() - _started_at, 1),
        "brainstem_ready": bool(
            LANE_CONFIG[Lane.BRAINSTEM]["model_path"]
            and os.path.isfile(LANE_CONFIG[Lane.BRAINSTEM]["model_path"])
        ),
        "llama_server_available": _llama_bin_path is not None,
    })


@_app.get("/v1/models")
async def http_list_models() -> JSONResponse:
    return JSONResponse(content={
        "object": "list",
        "data": [
            {
                "id": lane_cfg(Lane.BRAINSTEM)["file"],
                "object": "model",
                "created": int(_started_at),
                "owned_by": "ashatos",
            },
        ],
    })


@_app.post("/v1/chat/completions")
async def http_chat_completions(request: FastRequest) -> JSONResponse:
    return await _chat_completions_handler(request)


@_app.get("/", response_class=HTMLResponse)
async def http_landing() -> HTMLResponse:
    """Public-telemetry dashboard.

    Server-rendered HTML at request time; the embedded JS
    setInterval polls /api/dashboard_html every
    PUBLIC_REFRESH_SECONDS and updates the status row + the
    single BrainStem lane card in place. Replaces the previous
    Gradio `gr.Timer` behaviour with plain FastAPI + fetch.
    """
    return HTMLResponse(
        content=render_index_html(
            snapshot_provider=_snapshot,
            refresh_seconds=PUBLIC_REFRESH_SECONDS,
        )
    )


app = _app

_log.info(
    "FastAPI routes: /, /v1/chat/completions, /v1/models, /health, "
    "/api/public_status, /api/public_metrics, /api/dashboard_html"
)


if __name__ == "__main__":
    # Local dev only. HF Spaces (sdk: docker) runs uvicorn from
    # Dockerfile's ENTRYPOINT and never reaches this branch.
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860)
