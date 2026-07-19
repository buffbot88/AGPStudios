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

import gradio as gr
from fastapi import FastAPI, Request as FastRequest
from fastapi.responses import JSONResponse

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


# ── Gradio adapter ──────────────────────────────────────────────────────

def _gradio_lane_handler(lane: Lane):
    """Return a Gradio-callable handler for the given lane."""

    def handler(payload_json: str, request: gr.Request) -> str:
        # 1. Auth
        try:
            _KEY_GATE.check(headers_from_gradio(request), lane)
        except AuthError:
            return json.dumps({
                "ok": False,
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": "unauthorized",
                    "retryable": False,
                },
            })

        # 2. Parse body
        try:
            body = (
                json.loads(payload_json)
                if isinstance(payload_json, str)
                else payload_json
            ) or {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({
                "ok": False,
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Invalid JSON",
                    "retryable": False,
                },
            })

        # 3. Validate
        try:
            err = validate_request(body, lane)
            if err:
                raise InvalidRequestError(err)
        except InvalidRequestError as exc:
            return json.dumps({
                "ok": False,
                "request_id": str(uuid.uuid4()),
                "lane": lane.value,
                "error": exc.to_envelope(),
            })

        # 4. Run pipeline (lane is fixed by the route hint)
        result = execute_lane(lane.value, body)
        return json.dumps(result)

    return handler


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
# 10.  FastAPI / Gradio wiring
# ──────────────────────────────────────────────────────────────────────────

_fastapi_app = FastAPI(title="AshatOS Neural Host")


@_fastapi_app.post("/v1/chat/completions")
async def http_chat_completions(request: FastRequest) -> JSONResponse:
    return await _make_http_chat_completions()(request)


@_fastapi_app.get("/v1/models")
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


@_fastapi_app.get("/health")
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


@_fastapi_app.get("/api/public_status")
async def http_public_status() -> JSONResponse:
    return JSONResponse(content=_build_status())


@_fastapi_app.get("/api/public_metrics")
async def http_public_metrics() -> JSONResponse:
    return JSONResponse(content=_snapshot().render_metrics())


# ──────────────────────────────────────────────────────────────────────────
# 11.  Dashboard — redesigned neural host homepage (single BrainStem lane)
# ──────────────────────────────────────────────────────────────────────────

from dashboard import build_dashboard

def _build_gradio_blocks() -> "gr.Blocks":
    """Build the Gradio dashboard Blocks.

    Wrapped in a function -- not at module level -- so HF Spaces' static
    type-scanner cannot find a `gr.Blocks` instance in globals(). On a
    match HF launches its own Gradio runner with HF-injected auth on port
    7860, which is why our `/api/*` endpoints returned Gradio's login
    HTML instead of our JSON on the live Space. The builder below runs
    only at the moment of mount; the instance never enters the module's
    namespace.
    """
    with gr.Blocks(title="AshatOS Neural Host") as b:
        _tpl = build_dashboard(
            snapshot_provider=_snapshot,
            refresh_seconds=PUBLIC_REFRESH_SECONDS,
        )
    
        # Header (static, no refresh needed)
        gr.HTML(_tpl.header_html)
    
        # Status row (refreshed by timer)
        _status = gr.HTML(_tpl.status_html)
    
        # Single BrainStem lane card (refreshed by timer)
        with gr.Row(equal_height=True, variant="panel"):
            with gr.Column(scale=1, min_width=320):
                _brainstem = gr.HTML(_tpl.brainstem_html)
    
        # Live refresh via Gradio Timer (spec \u00a710)
        _timer = gr.Timer(value=_tpl.refresh_seconds, active=True)
        _timer.tick(
            fn=_tpl.refresh_fn,
            inputs=None,
            outputs=[_status, _brainstem],
            queue=False,
            show_progress="hidden",
        )
    
        # Footer
        gr.HTML(
            """
            <div style="text-align: center; padding: 16px 20px 24px;">
              <span style="font-size: 0.68em; color: #64748B; font-family: sans-serif;
                   letter-spacing: 0.03em;">
                BrainStem inference engine \u00b7 Public telemetry only</span>
            </div>
            """
        )
    
        # -- Private Gradio API endpoints (AshatOS communication only) --
        _brainstem_input = gr.Textbox(visible=False, value="{}", label="brainstem_payload")
        _brainstem_trigger = gr.Button(visible=False, elem_id="_brainstem_trigger")
        _brainstem_trigger.click(
            fn=_gradio_lane_handler(Lane.BRAINSTEM),
            inputs=[_brainstem_input],
            outputs=[gr.Textbox(visible=False)],
            api_name="brainstem",
            concurrency_limit=1,
        )
    
        _status_trigger = gr.Button(visible=False, elem_id="_status_trigger")
        _status_trigger.click(
            fn=_public_status_json,
            inputs=[],
            outputs=[gr.Textbox(visible=False)],
            api_name="public_status",
            concurrency_limit=1,
        )
    
        _metrics_trigger = gr.Button(visible=False, elem_id="_metrics_trigger")
        _metrics_trigger.click(
            fn=_public_metrics_json,
            inputs=[],
            outputs=[gr.Textbox(visible=False)],
            api_name="public_metrics",
            concurrency_limit=1,
        )
    
        b.queue(default_concurrency_limit=1, max_size=QUEUE_LIMIT)
    return b


# Mapping of binary-install failure codes -> exception class. Used by
# startup() so the dashboard surfaces the typed cause (HF credits vs
# rate limit vs generic install fail) instead of one blanket 503.
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
# 12.  Launch — HF Spaces serves the demo. Gradio's internal FastAPI app
#      (demo.app) is available after queue() is called. We add the
#      OpenAI-compatible /v1/chat/completions route to it so AshatOS
#      can connect with the standard OpenAI format.
# ──────────────────────────────────────────────────────────────────────────# Mount Gradio inside the SAME FastAPI so user routes share one port with
# Gradio's UI / WS / queue. `gr.mount_gradio_app(...)` mutates `_fastapi_app`
# in place (verified empirically: `gr.mount_gradio_app(...) is _fastapi_app`
# is True) and returns the same FastAPI object, so `app` and `_fastapi_app`
# end up aliased to one object holding our decorator routes AND the Gradio
# Mount at path="/".
#
# The `blocks=_build_gradio_blocks()` argument calls the builder function
# inline. Inside that function the `gr.Blocks` instance never enters
# module globals() which is what HF Spaces' static scanner iterates to
# decide whether to launch its own parallel Gradio runner (with auth).
# Without this lazy-build escape, the only top-level binding HF sees is
# `app` -- the right thing. With it, every /api/* request hits our
# FastAPI route directly on port 7860 instead of Gradio's auth-shim.
app = gr.mount_gradio_app(
    app=_fastapi_app,
    blocks=_build_gradio_blocks(),
    path="/",
)

# Defensive verification (cheap, future-proof against Gradio's mount
# behaviour silently stripping pre-existing routes). Both `APIRoute` and
# `Route` (Starlette) names appear on `app.routes` after mount -- the
# former for our `@_fastapi_app.get/post` decorators, the latter for
# FastAPI's auto-docs routes at `/docs`, `/openapi.json`, etc.
#
# We do NOT use ``assert`` here -- an AssertionError on import would crash
# the whole module and either trip HF Spaces' restart loop (ASGI mode) or
# leave the script-mode container exiting non-zero. Logging the regression
# at ERROR level instead surfaces the same warning through the dashboard's
# event log + logs tab while keeping port 7860 bound so the Space stays
# RUNNING. An operator can then read the diagnostic line and pin a Gradio
# version that matches the operator's tolerance.
if not any(
    r.__class__.__name__ in ("APIRoute", "Route")
    and getattr(r, "path", None) == "/health"
    for r in app.routes
):
    _log.error(
        "DEFECTIVE FASTAPI MOUNT: /health missing from app.routes -- a future "
        "Gradio release likely stripped pre-existing routes on mount. /health, "
        "/v1/*, /api/* will 404 until a compatible Gradio is re-installed."
    )

_log.info(
    "FastAPI routes mounted via gr.mount_gradio_app: /v1/chat/completions, "
    "/v1/models, /health, /api/public_status, /api/public_metrics"
)

# Hugging Face Spaces has two runner modes for app.py. If both ASGI and
# SCRIPT modes fire, exactly one uvicorn wins port 7860; the other raises
# OSError 98. We MUST NOT exit non-zero in that case -- the winning uvicorn
# is serving correctly and a non-zero container exit would trip HF's
# FAIL state regardless. Swallow the OSError and hold the script-mode
# process alive so HF keeps the Space RUNNING until it restarts the
# container for us. HF sends SIGTERM/SIGKILL on restart (never SIGINT) so
# we leave Ctrl+C untouched for local dev.
if __name__ == "__main__":
    import time as _time
    import uvicorn
    try:
        uvicorn.run(app, host="0.0.0.0", port=7860)
    except OSError as exc:
        _log.info(
            "script-mode could not bind 7860 (%s) -- HF Spaces' ASGI-mode "
            "uvicorn is already serving the same mounted app; sleeping "
            "to keep the container healthy until HF restarts us.",
            exc,
        )
        while True:
            _time.sleep(86400)  # 1 day per iteration; safe on Windows (no Sleep() DWORD overflow)
