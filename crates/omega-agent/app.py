#!/usr/bin/env python3
"""AshatOS Neural I/O Host — slim orchestrator.

The heavy lifting now lives in purpose-built modules:
    * :mod:`domain`            — Lane enum + per-lane config
    * :mod:`run_errors`        — typed exception hierarchy + RunError→JSON codes
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
from installer import ensure_llama_server
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
    InferenceUnavailableError,
    InvalidRequestError,
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
# GPU slot durations for ZeroGPU. Read once at import time and exposed
# as plain module-level Names so the @spaces.GPU decorators below are
# trivially AST-readable by HF Spaces' static scanner (a function-call
# inside the decorator arg can hang the scanner with the
# "No @spaces.GPU function detected during startup" error).
_MICRO_GPU_DURATION = int(os.getenv("MICRO_GPU_DURATION", "60"))
_MAIN_GPU_DURATION = int(os.getenv("MAIN_GPU_DURATION", "120"))


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
# 5.  Request validation
# ──────────────────────────────────────────────────────────────────────────

def validate_request(body: dict[str, Any], lane: Lane) -> str | None:
    cfg = lane_cfg(lane)
    messages = body.get("messages", [])
    if not messages or not isinstance(messages, list):
        return "Missing or invalid 'messages' field"
    if len(messages) > cfg["max_messages"]:
        return f"Too many messages (max {cfg['max_messages']})"
    body_bytes = len(json.dumps(body))
    if body_bytes > cfg["max_body_bytes"]:
        return f"Request body too large (max {cfg['max_body_bytes']} bytes)"
    for msg in messages:
        if not isinstance(msg, dict):
            return "Each message must be a dict"
        role = msg.get("role", "")
        if role not in ("system", "user", "assistant"):
            return f"Unsupported role: {role}"
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.strip():
            return "Message content must be a non-empty string"
    max_tokens = body.get("max_tokens", 0)
    if max_tokens and (not isinstance(max_tokens, (int, float)) or max_tokens < 1):
        return "max_tokens must be a positive integer"
    temperature = body.get("temperature", 0.7)
    if isinstance(temperature, (int, float)) and (temperature < 0 or temperature > 2):
        return "temperature must be between 0 and 2"
    top_p = body.get("top_p", 0.9)
    if isinstance(top_p, (int, float)) and (top_p < 0 or top_p > 1):
        return "top_p must be between 0 and 1"
    if body.get("stream", False):
        return "Streaming is not yet supported"
    return None


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
            "%s: inference unavailable — llama-server binary not installed",
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


def _make_internal_error(message: str):
    """Construct an ad-hoc RunError for the broadest catch path."""
    err = RunError(message)
    err.code = "INTERNAL_ERROR"
    err.http_status = 500
    err.retryable = True
    return err


# ──────────────────────────────────────────────────────────────────────────
# 7.  @spaces.GPU wrappers — one entry per lane
# ──────────────────────────────────────────────────────────────────────────

@spaces.GPU
def _execute_microbrain_gpu(payload: dict[str, Any]) -> dict[str, Any]:
    return _run_pipeline(Lane.MICROBRAIN, payload)


@spaces.GPU
def _execute_mainbrain_gpu(payload: dict[str, Any]) -> dict[str, Any]:
    return _run_pipeline(Lane.MAINBRAIN, payload)


# ──────────────────────────────────────────────────────────────────────────
# 7b.  Metric recording in the main process
#      The functions above may run in a ZeroGPU worker (separate process).
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
    """Serializing entry point — one inference at a time across the Space.

    The ``@spaces.GPU`` functions run in an isolated worker process (on
    HF Spaces with ZeroGPU).  We record metrics here, in the *main*
    process, after the result returns, so the dashboard timer can read
    them from the same in-memory ``METRICS`` singleton.
    """
    lane = Lane.parse(lane_str)
    with _inference_lock:
        if lane is Lane.MICROBRAIN:
            result = _execute_microbrain_gpu(payload)
        else:
            result = _execute_mainbrain_gpu(payload)

    _record_returned_result(lane, result)
    return result


# ──────────────────────────────────────────────────────────────────────────
# 8.  Surface adapters — fastapi (HTTP) and gradio (queue API)
# ──────────────────────────────────────────────────────────────────────────

def _envelope_to_response(envelope: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Backwards-compat shim — see :func:`response_adapter.envelope_to_response`."""
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

        # 2. Lane resolution (no string-sniff — exact alias match)
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
                "id": lane_cfg(Lane.MAINBRAIN)["file"],
                "object": "model",
                "created": int(_started_at),
                "owned_by": "ashatos",
            },
            {
                "id": lane_cfg(Lane.MICROBRAIN)["file"],
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
        "microbrain_ready": bool(
            LANE_CONFIG[Lane.MICROBRAIN]["model_path"]
            and os.path.isfile(LANE_CONFIG[Lane.MICROBRAIN]["model_path"])
        ),
        "mainbrain_ready": bool(
            LANE_CONFIG[Lane.MAINBRAIN]["model_path"]
            and os.path.isfile(LANE_CONFIG[Lane.MAINBRAIN]["model_path"])
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
# 11.  Dashboard — redesigned neural host homepage (spec §4–§15)
# ──────────────────────────────────────────────────────────────────────────

from dashboard import build_dashboard

with gr.Blocks(title="AshatOS Neural Host") as _demo:
    _tpl = build_dashboard(
        snapshot_provider=_snapshot,
        refresh_seconds=PUBLIC_REFRESH_SECONDS,
    )

    # Header (static, no refresh needed)
    gr.HTML(_tpl.header_html)

    # Status row (refreshed by timer)
    _status = gr.HTML(_tpl.status_html)

    # Two-lane card layout (spec §3) — both cards refreshed by timer
    with gr.Row(equal_height=True, variant="panel"):
        with gr.Column(scale=1, min_width=320):
            _micro = gr.HTML(_tpl.micro_html)
        with gr.Column(scale=1, min_width=320):
            _main = gr.HTML(_tpl.main_html)

    # Live refresh via Gradio Timer (spec §10)
    _timer = gr.Timer(value=_tpl.refresh_seconds, active=True)
    _timer.tick(
        fn=_tpl.refresh_fn,
        inputs=None,
        outputs=[_status, _micro, _main],
        queue=False,
        show_progress="hidden",
    )

    # Footer
    gr.HTML(
        """
        <div style="text-align: center; padding: 16px 20px 24px;">
          <span style="font-size: 0.68em; color: #64748B; font-family: sans-serif;
               letter-spacing: 0.03em;">
            Private inference lanes · Public telemetry only</span>
        </div>
        """
    )

    # -- Private Gradio API endpoints (AshatOS communication only) --
    _micro_input = gr.Textbox(visible=False, value="{}", label="microbrain_payload")
    _micro_trigger = gr.Button(visible=False, elem_id="_micro_trigger")
    _micro_trigger.click(
        fn=_gradio_lane_handler(Lane.MICROBRAIN),
        inputs=[_micro_input],
        outputs=[gr.Textbox(visible=False)],
        api_name="microbrain",
        concurrency_limit=1,
    )

    _main_input = gr.Textbox(visible=False, value="{}", label="mainbrain_payload")
    _main_trigger = gr.Button(visible=False, elem_id="_main_trigger")
    _main_trigger.click(
        fn=_gradio_lane_handler(Lane.MAINBRAIN),
        inputs=[_main_input],
        outputs=[gr.Textbox(visible=False)],
        api_name="mainbrain",
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


def startup() -> None:
    global _llama_bin_path
    _log.info("=" * 60)
    _log.info("AshatOS Neural I/O Host — Dual-Lane Inference")
    _log.info("=" * 60)

    _llama_bin_path = ensure_llama_server()
    if _llama_bin_path:
        _log.info("llama-server binary: %s", _llama_bin_path)
    else:
        _log.warning("llama-server binary not available — degraded mode")

    if _llama_bin_path:
        for lane in (Lane.MICROBRAIN, Lane.MAINBRAIN):
            try:
                _BACKEND_LAUNCHER.ensure_model(lane)
                _log.info("%s model cached: %s", lane.value,
                          LANE_CONFIG[lane]["model_path"])
            except Exception as exc:
                _log.warning("%s model pre-download failed: %s",
                             lane.value, exc)


startup()

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
# ──────────────────────────────────────────────────────────────────────────

_demo.queue(default_concurrency_limit=1, max_size=QUEUE_LIMIT)

# Register the /v1/chat/completions endpoint on Gradio's internal app
# so it's served on the same port as the Gradio UI.
_GRADIO_INTERNAL = getattr(_demo, "app", None)
if _GRADIO_INTERNAL is not None:
    from fastapi import APIRouter
    _router = APIRouter()
    for route in _fastapi_app.routes:
        _router.routes.append(route)
    _GRADIO_INTERNAL.include_router(_router)
    _log.info("FastAPI routes mounted on Gradio internal app")
else:
    _log.warning("Gradio internal app not available — FastAPI routes not served")

app = _demo

if __name__ == "__main__":
    _demo.launch(server_name="0.0.0.0", server_port=7860,
                   show_error=True, theme=gr.themes.Soft())
