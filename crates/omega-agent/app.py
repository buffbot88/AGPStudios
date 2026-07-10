#!/usr/bin/env python3
"""AshatOS Neural I/O Host — slim orchestrator.

The heavy lifting now lives in purpose-built modules:
    * :mod:`domain`            — Lane enum + per-lane config + request validation
    * :mod:`run_errors`        — typed exception hierarchy + RunError->JSON codes
    * :mod:`lane_resolver`     — strict route-or-model lane routing
    * :mod:`lane_keygate`      — single auth authority for both surfaces
    * :mod:`backend_launcher`  — per-request llama-server lifecycle
    * :mod:`completion_client` — HTTP-only client to the live backend
    * :mod:`run_metrics`       — sanitized metric + event recording
    * :mod:`metrics_store`     — thread-safe in-memory rolling deque
    * :mod:`installer`         — bin installer + GitHub/HF mirror tiers
    * :mod:`run_queue`         — inference queue with timeout + depth tracking
    * :mod:`surface_adapter`   — transport-agnostic pipeline seam
    * :mod:`response_adapter`  — envelope to HTTP response conversion
    * :mod:`env_scanner`       — ZeroGPU runtime diagnostics probe
    * :mod:`public_snapshot`   — one canonical projection, three consumers

What stays here: logging, config defaults, the FastAPI and Gradio wiring,
the :func:`_run_pipeline` orchestrator and envelope builders,
the @spaces.GPU decorated handlers, the Gradio dashboard,
:func:`execute_lane` (serialising entry point), and lazy initialization
(backgrounded so /health responds immediately and the UI renders before
models or the llama-server binary are ready).
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
from typing import Any

import gradio as gr
from fastapi import FastAPI, Request as FastRequest
from fastapi.responses import JSONResponse

from backend_launcher import BackendLauncher, LiveBackend
from completion_client import CompletionClient, CompletionResult
from domain import LANE_CONFIG, Lane, lane_cfg, validate_request
from installer import ensure_llama_server
from lane_keygate import LaneKeyGate
from lane_resolver import LaneResolver
from metrics_store import METRICS
from run_errors import (
    InferenceUnavailableError,
    RunError,
)
from run_metrics import RunMetrics
from run_queue import RunQueue, RunQueueTimeout
from surface_adapter import (
    FastAPISurfaceAdapter,
    GradioSurfaceAdapter,
    run_surface,
)
from lazy_init import run_lazy_init, bin_path, init_done, init_error

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


# ──────────────────────────────────────────────────────────────────────────
# 3.  Global runtime state — all mutable, init'd to safe defaults
# ──────────────────────────────────────────────────────────────────────────

_started_at: float = time.time()
_RUN_QUEUE = RunQueue(timeout_s=300.0)
_active_processes: list[subprocess.Popen[str]] = []


def _binary_path_getter() -> str | None:
    return bin_path()


# Pipeline collaborators instantiated once at module import (no I/O).
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
# 5.  Run pipeline — the slim orchestrator
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
    """Slim Run. Composes :class:`BackendLauncher`, :class:`CompletionClient`,
    and :class:`RunMetrics` into one request lifecycle.
    """
    request_id = str(payload.get("request_id") or uuid.uuid4())
    payload.setdefault("request_id", request_id)

    started_at = time.perf_counter()
    cold_start = _is_cold_start(lane)

    if not bin_path():
        _log.warning(
            "%s: inference unavailable — llama-server binary not installed",
            lane.value,
        )
        exc = InferenceUnavailableError(
            "llama-server binary not installed (degraded mode)"
        )
        elapsed = round((time.perf_counter() - started_at) * 1000, 1)
        _RUN_METRICS.record_failure(
            lane, request_id, exc, elapsed, cold_start=cold_start,
        )
        return _build_failure_envelope(lane, request_id, exc)

    try:
        with _BACKEND_LAUNCHER.launch(lane) as backend:
            _active_processes.append(backend.process)
            try:
                completion = _COMPLETION_CLIENT.complete(backend, lane, payload)
            finally:
                try:
                    _active_processes.remove(backend.process)
                except ValueError:
                    pass
        total_ms = round((time.perf_counter() - started_at) * 1000, 1)
        _RUN_METRICS.record_success(
            lane, backend, completion, total_ms, cold_start=cold_start,
        )
        return _build_success_envelope(
            lane, request_id, backend, completion, total_ms, cold_start,
        )

    except RunError as exc:
        total_ms = round((time.perf_counter() - started_at) * 1000, 1)
        _RUN_METRICS.record_failure(
            lane, request_id, exc, total_ms, cold_start=cold_start,
        )
        return _build_failure_envelope(lane, request_id, exc)

    except Exception as exc:
        _log.exception("%s: unhandled exception in run pipeline", lane.value)
        total_ms = round((time.perf_counter() - started_at) * 1000, 1)
        envelope = {
            "code": "INTERNAL_ERROR",
            "message": str(exc)[:200],
            "retryable": True,
        }
        _RUN_METRICS.record_failure(
            lane, request_id,
            _make_internal_error(str(exc)),
            total_ms, cold_start=cold_start,
        )
        return {
            "ok": False, "request_id": request_id, "lane": lane.value,
            "error": envelope,
        }


def _make_internal_error(message: str):
    err = RunError(message)
    err.code = "INTERNAL_ERROR"
    err.http_status = 500
    err.retryable = True
    return err


# ──────────────────────────────────────────────────────────────────────────
# 6.  @spaces.GPU decorated handlers
# ──────────────────────────────────────────────────────────────────────────

_GRADIO_ADAPTER = GradioSurfaceAdapter()
_FASTAPI_ADAPTER = FastAPISurfaceAdapter()


def _gradio_shared(payload_json: str, request: gr.Request, lane: Lane) -> str:
    headers = _GRADIO_ADAPTER.extract_headers(request)
    body: dict[str, Any] | None = None
    parse_failed = False
    try:
        body = (
            json.loads(payload_json) if isinstance(payload_json, str) else payload_json
        ) or {}
    except (json.JSONDecodeError, TypeError):
        parse_failed = True

    envelope = run_surface(
        headers=headers,
        body=body,
        body_parse_failed=parse_failed,
        key_gate=_KEY_GATE,
        execute_fn=execute_lane,
        lane=lane,
    )
    if envelope.get("ok", False):
        return _GRADIO_ADAPTER.respond_ok(envelope)
    return _GRADIO_ADAPTER.respond_error(400, envelope)


@spaces.GPU
def gradio_microbrain(payload_json: str, request: gr.Request) -> str:
    return _gradio_shared(payload_json, request, Lane.MICROBRAIN)


@spaces.GPU
def gradio_mainbrain(payload_json: str, request: gr.Request) -> str:
    return _gradio_shared(payload_json, request, Lane.MAINBRAIN)


# Dashboard handlers — also @spaces.GPU decorated so the ZeroGPU
# platform scanner finds a decorated function for every Gradio event.
@spaces.GPU
def gradio_status() -> str:
    return _status_html()


@spaces.GPU
def gradio_metrics_load() -> tuple:
    return _refresh_metrics_body()


@spaces.GPU
def gradio_lazy_init() -> str:
    return run_lazy_init(
        gradio_microbrain=gradio_microbrain,
        gradio_mainbrain=gradio_mainbrain,
        _fastapi_sync_inference=_fastapi_sync_inference,
    )


def execute_lane(lane_str: str, payload: dict[str, Any]) -> dict[str, Any]:
    lane = Lane.parse(lane_str)
    try:
        with _RUN_QUEUE.acquire(lane):
            return _run_pipeline(lane, payload)
    except RunQueueTimeout:
        _log.warning("%s: inference queue timeout", lane.value)
        exc = InferenceUnavailableError("inference queue timeout")
        return _build_failure_envelope(lane, str(uuid.uuid4()), exc)


# ── FastAPI adapter ─────────────────────────────────────────────────────


@spaces.GPU
def _fastapi_sync_inference(
    headers: dict[str, str],
    body: dict[str, Any] | None,
    parse_failed: bool,
) -> dict[str, Any]:
    return run_surface(
        headers=headers,
        body=body,
        body_parse_failed=parse_failed,
        key_gate=_KEY_GATE,
        execute_fn=execute_lane,
        resolver=_RESOLVER,
    )


async def _handle_http_chat_completions(request: FastRequest) -> JSONResponse:
    headers = _FASTAPI_ADAPTER.extract_headers(request)
    body: dict[str, Any] | None = None
    parse_failed = False
    try:
        body = await request.json()
    except Exception:
        parse_failed = True

    loop = asyncio.get_event_loop()
    envelope = await loop.run_in_executor(
        None,
        lambda: _fastapi_sync_inference(headers, body, parse_failed),
    )
    if envelope.get("ok", False):
        return _FASTAPI_ADAPTER.respond_ok(envelope)
    return _FASTAPI_ADAPTER.respond_error(400, envelope)


# ──────────────────────────────────────────────────────────────────────────
# 7.  Public snapshot — cheap, no I/O
# ──────────────────────────────────────────────────────────────────────────

from public_snapshot import PublicSnapshot, RuntimeState


def _snapshot() -> PublicSnapshot:
    bp = bin_path()
    return PublicSnapshot.from_metrics(
        METRICS,
        RuntimeState(
            started_at=_started_at,
            llama_server_available=bp is not None,
            llama_server_path=bp,
        ),
        LANE_CONFIG,
    )


def _build_status() -> dict[str, Any]:
    return _snapshot().render_status()


def _public_status_json() -> str:
    return json.dumps(_snapshot().render_status())


def _public_metrics_json() -> str:
    return json.dumps(_snapshot().render_metrics())


def _status_html() -> str:
    return _snapshot().render_html()


def _refresh_metrics_body() -> tuple:
    frames = _snapshot().render_frames()
    return (
        frames["microbrain"],
        frames["microbrain"],
        frames["mainbrain"],
        frames["mainbrain"],
        frames["events"],
    )


# ──────────────────────────────────────────────────────────────────────────
# 8.  FastAPI / Gradio wiring
#     ⚠️  Everything below must complete quickly. No blocking I/O.
# ──────────────────────────────────────────────────────────────────────────

_fastapi_app = FastAPI(title="AshatOS Neural Host")


# ── /health — must ALWAYS respond immediately, no imports, no I/O ──────

@_fastapi_app.get("/health")
async def http_health() -> JSONResponse:
    """Boring, static health check — no I/O, no locks, no imports."""
    return JSONResponse(content={
        "status": "ok",
        "app": "ashat-neural-host",
        "uptime_seconds": round(time.time() - _started_at, 1),
        "llama_server_available": bin_path() is not None,
        "init_done": init_done(),
        "init_error": init_error(),
    })


@_fastapi_app.post("/v1/chat/completions")
async def http_chat_completions(request: FastRequest) -> JSONResponse:
    return await _handle_http_chat_completions(request)


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


@_fastapi_app.get("/api/public_status")
async def http_public_status() -> JSONResponse:
    return JSONResponse(content=_build_status())


@_fastapi_app.get("/api/public_metrics")
async def http_public_metrics() -> JSONResponse:
    return JSONResponse(content=_snapshot().render_metrics())


# === Gradio dashboard ===

JAVASCRIPT_REFRESH = f"""
<script>
setInterval(function() {{
    var btn = document.querySelector('#refresh-status-btn');
    if (btn) btn.click();
}}, {PUBLIC_REFRESH_SECONDS * 1000});
</script>
"""



with gr.Blocks(title="AshatOS Neural Host") as _demo:
    gr.HTML(
        """
        <div style="text-align: center; padding: 20px;">
            <h1 style="margin: 0; font-size: 2em;">🧠 ASHAT NEURAL HOST</h1>
            <p style="color: #888; font-size: 1.1em;">Dual-Lane Inference Telemetry</p>
        </div>
        """
    )

    status_display = gr.HTML(value=_status_html())

    with gr.Row():
        refresh_btn = gr.Button(
            "🔄 Refresh Status",
            variant="secondary",
            elem_id="refresh-status-btn",
        )

    gr.Markdown("## Performance Metrics")

    with gr.Tabs():
        with gr.TabItem("MicroBrain"):
            micro_gen_plot = gr.LinePlot(
                x="timestamp", y="generation_tokens_per_second",
                title="Generation Tokens/sec (MicroBrain)",
            )
            micro_latency_plot = gr.LinePlot(
                x="timestamp", y="total_latency_ms",
                title="Total Latency (MicroBrain)",
            )
        with gr.TabItem("MainBrain"):
            main_gen_plot = gr.LinePlot(
                x="timestamp", y="generation_tokens_per_second",
                title="Generation Tokens/sec (MainBrain)",
            )
            main_latency_plot = gr.LinePlot(
                x="timestamp", y="total_latency_ms",
                title="Total Latency (MainBrain)",
            )

    gr.Markdown("## Recent Events")
    events_display = gr.Dataframe(
        headers=["Event"],
        label="Recent Events",
        row_count=10,
    )

    with gr.Accordion("Configuration", open=False):
        gr.Markdown(f"""
        ### Lane Configuration

        | Setting | MicroBrain | MainBrain |
        |---|---|---|
        | Model | `{lane_cfg(Lane.MICROBRAIN)['file']}` | `{lane_cfg(Lane.MAINBRAIN)['file']}` |
        | Context | {lane_cfg(Lane.MICROBRAIN)['ctx']} | {lane_cfg(Lane.MAINBRAIN)['ctx']} |
        | Max tokens | {lane_cfg(Lane.MICROBRAIN)['max_tokens']} | {lane_cfg(Lane.MAINBRAIN)['max_tokens']} |
        | GPU duration | {lane_cfg(Lane.MICROBRAIN)['gpu_duration']}s | {lane_cfg(Lane.MAINBRAIN)['gpu_duration']}s |

        ### Runtime

        - `LLAMA_SERVER_PORT`: {LLAMA_SERVER_PORT}
        - `N_THREADS`: {N_THREADS} | `N_BATCH`: {N_BATCH}
        - `PUBLIC_REFRESH_SECONDS`: {PUBLIC_REFRESH_SECONDS}
        - `QUEUE_LIMIT`: {QUEUE_LIMIT}
        """)

    refresh_btn.click(
        fn=gradio_status,
        inputs=[],
        outputs=status_display,
        api_name="status",
        concurrency_limit=1,
    )

    # Lazy init — runs in Gradio's queue when UI loads.
    # Must be @spaces.GPU-decorated so the ZeroGPU scanner finds it.
    _demo.load(fn=gradio_lazy_init, inputs=None, outputs=None, concurrency_limit=1)

    _demo.load(
        fn=gradio_metrics_load,
        inputs=[],
        outputs=[
            micro_gen_plot, micro_latency_plot,
            main_gen_plot, main_latency_plot,
            events_display,
        ],
        concurrency_limit=1,
    )

    _micro_input = gr.Textbox(visible=False, value="{}", label="microbrain_payload")
    _micro_trigger = gr.Button(visible=False, elem_id="_micro_trigger")
    _micro_trigger.click(
        fn=gradio_microbrain,
        inputs=[_micro_input],
        outputs=[gr.Textbox(visible=False)],
        api_name="microbrain",
        concurrency_limit=1,
    )

    _main_input = gr.Textbox(visible=False, value="{}", label="mainbrain_payload")
    _main_trigger = gr.Button(visible=False, elem_id="_main_trigger")
    _main_trigger.click(
        fn=gradio_mainbrain,
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


# ── Sync ZeroGPU startup report ──────────────────────────────────────
# The platform waits for this report to confirm @spaces.GPU functions
# are registered. Called here (after ALL handlers exist, before the app
# is mounted) so it completes before the platform health-check fires.
try:
    from spaces.config import Config as _SC
    if _SC.zero_gpu:
        from spaces.zero import client as _zclient
        _zclient.startup_report()
        _log.info("sync startup_report sent")
except Exception as exc:
    _log.warning("sync startup_report failed (non-fatal): %s", exc)


# ──────────────────────────────────────────────────────────────────────────
# 10.  Mount Gradio on FastAPI — completes quickly, no blocking I/O.
# ──────────────────────────────────────────────────────────────────────────

_demo.queue(default_concurrency_limit=1, max_size=QUEUE_LIMIT)

app = gr.mount_gradio_app(
    _fastapi_app, _demo, path="/",
    theme=gr.themes.Soft(),
    head=JAVASCRIPT_REFRESH,
)


if __name__ == "__main__":
    if not os.getenv("SPACE_ID"):  # HF Spaces auto-serves the app
        import uvicorn
        # Note: on HF Spaces the platform manages the server.
        # This block only runs in local dev.
        uvicorn.run(app, host="0.0.0.0", port=7860)
