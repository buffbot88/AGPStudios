#!/usr/bin/env python3
"""
AshatOS Neural I/O Host

A Hugging Face Space providing two private inference lanes (MicroBrain /
MainBrain) behind authenticated API endpoints, with a public read-only
telemetry dashboard.  Each inference request starts llama-server on demand,
runs one completion, collects metrics, and terminates.

OpenAI-compatible endpoints (fastapi):
    GET  /v1/models              → list available models (no auth)
    POST /v1/chat/completions    → chat completions (X-Ashat-Key header)

Gradio API endpoints (queue-based):
    POST /gradio_api/call/microbrain   → MicroBrain inference
    POST /gradio_api/call/mainbrain    → MainBrain inference

Public (read-only):
    GET  /                         → Gradio telemetry dashboard
"""

from __future__ import annotations

import asyncio
import atexit
import hmac
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import gradio as gr
import requests
from fastapi import FastAPI, Request as FastRequest
from fastapi.responses import JSONResponse
from huggingface_hub import hf_hub_download

# ZeroGPU compatibility — direct @spaces.GPU decorator (needed for static detection)
try:
    import spaces
except ImportError:
    import types as _types
    spaces = _types.ModuleType("spaces")
    class _GPU:
        """No-op GPU decorator for non-ZeroGPU environments."""
        def __call__(self, fn=None, **kwargs):
            if fn is not None:
                return fn
            return lambda f: f
    spaces.GPU = _GPU()  # type: ignore[assignment]

# ──────────────────────────────────────────────────────────────────────────
# 1.  Logging (stdout only — no disk writes)
# ──────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
_log = logging.getLogger("ashatos")

# ──────────────────────────────────────────────────────────────────────────
# 2.  Configuration (all overridable via env vars / Space secrets)
# ──────────────────────────────────────────────────────────────────────────

# Models (downloaded from HF Hub on first inference)
MAIN_MODEL_REPO = os.getenv("MAIN_MODEL_REPO", "RipBuffy/LFM2.5-Q6_K")
MAIN_MODEL_FILE = os.getenv("MAIN_MODEL_FILE", "LFM2.5-1.2B-Instruct-Q6_K.gguf")
MICRO_MODEL_REPO = os.getenv("MICRO_MODEL_REPO", "RipBuffy/LFM2.5-Q6_K")
MICRO_MODEL_FILE = os.getenv("MICRO_MODEL_FILE", "LFM2.5-350M-Q6_K.gguf")
MODEL_REVISION = os.getenv("MODEL_REVISION", "main")
HF_TOKEN: str | None = os.getenv("HF_TOKEN") or None

# Runtime
LLAMA_SERVER_PORT = int(os.getenv("LLAMA_SERVER_PORT", "18080"))
N_THREADS = int(os.getenv("N_THREADS", "2"))
N_BATCH = int(os.getenv("N_BATCH", "128"))
MAIN_CTX = int(os.getenv("MAIN_CTX", "1536"))
MICRO_CTX = int(os.getenv("MICRO_CTX", "1024"))
MAIN_MAX_TOKENS = int(os.getenv("MAIN_MAX_TOKENS", "256"))
MICRO_MAX_TOKENS = int(os.getenv("MICRO_MAX_TOKENS", "128"))
MAIN_GPU_DURATION = int(os.getenv("MAIN_GPU_DURATION", "120"))
MICRO_GPU_DURATION = int(os.getenv("MICRO_GPU_DURATION", "60"))
QUEUE_LIMIT = int(os.getenv("QUEUE_LIMIT", "16"))
PUBLIC_REFRESH_SECONDS = int(os.getenv("PUBLIC_REFRESH_SECONDS", "10"))
LLAMA_SERVER_VERSION = os.getenv("LLAMA_SERVER_VERSION", "latest")

# Lane definitions
LANES: dict[str, dict[str, Any]] = {
    "mainbrain": {
        "label": "MainBrain",
        "repo": MAIN_MODEL_REPO,
        "file": MAIN_MODEL_FILE,
        "ctx": MAIN_CTX,
        "max_tokens": MAIN_MAX_TOKENS,
        "gpu_duration": MAIN_GPU_DURATION,
        "max_messages": 64,
        "max_body_bytes": 262_144,
        "model_path": "",
    },
    "microbrain": {
        "label": "MicroBrain",
        "repo": MICRO_MODEL_REPO,
        "file": MICRO_MODEL_FILE,
        "ctx": MICRO_CTX,
        "max_tokens": MICRO_MAX_TOKENS,
        "gpu_duration": MICRO_GPU_DURATION,
        "max_messages": 32,
        "max_body_bytes": 131_072,
        "model_path": "",
    },
}

# Authentication keys (Space secrets — all communication via AshatOS)
_ASHAT_MICRO_KEY: str = os.getenv("ASHAT_MICROBRAIN_KEY", "")
_ASHAT_MAIN_KEY: str = os.getenv("ASHAT_MAINBRAIN_KEY", "")

# ──────────────────────────────────────────────────────────────────────────
# 3.  Global state
# ──────────────────────────────────────────────────────────────────────────

_started_at: float = time.time()
_inference_lock = threading.Lock()
_metrics_lock = threading.Lock()
_downloaded_models: dict[str, str] = {}
_llama_bin_path: str | None = None
_active_processes: list[subprocess.Popen[str]] = []

# ──────────────────────────────────────────────────────────────────────────
# 4.  Metrics store (thread-safe, in-memory rolling deque — no disk writes)
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class MetricRecord:
    timestamp: str = ""
    lane: str = ""
    success: bool = True
    cold_start: bool = False
    server_start_ms: float = 0.0
    model_load_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_per_second: float = 0.0
    generation_tokens_per_second: float = 0.0
    time_to_first_token_ms: float | None = None
    total_latency_ms: float = 0.0
    backend: str = "cuda"
    gpu_offload_verified: bool = True
    finish_reason: str = "stop"
    error_category: str | None = None


class MetricsStore:
    """Thread-safe in-memory rolling metrics store (no disk writes)."""

    def __init__(self, maxlen: int = 500, event_maxlen: int = 200) -> None:
        self._maxlen = maxlen
        self._microbrain: deque[MetricRecord] = deque(maxlen=maxlen)
        self._mainbrain: deque[MetricRecord] = deque(maxlen=maxlen)
        self._events: deque[str] = deque(maxlen=event_maxlen)

    def record(self, rec: MetricRecord) -> None:
        with _metrics_lock:
            if rec.lane == "microbrain":
                self._microbrain.append(rec)
            else:
                self._mainbrain.append(rec)

    def add_event(self, event: str) -> None:
        with _metrics_lock:
            ts = datetime.now(timezone.utc).isoformat()
            self._events.append(f"[{ts}] {event}")

    def get_lane_metrics(self, lane: str) -> list[MetricRecord]:
        with _metrics_lock:
            if lane == "microbrain":
                return list(self._microbrain)
            return list(self._mainbrain)

    def get_all_metrics(self) -> dict[str, list[MetricRecord]]:
        with _metrics_lock:
            return {
                "microbrain": list(self._microbrain),
                "mainbrain": list(self._mainbrain),
            }

    def get_events(self) -> list[str]:
        with _metrics_lock:
            return list(self._events)

    def clear(self) -> None:
        with _metrics_lock:
            self._microbrain.clear()
            self._mainbrain.clear()
            self._events.clear()

    def get_summary(self, lane: str) -> dict[str, Any]:
        records = self.get_lane_metrics(lane)
        if not records:
            return {
                "total_requests": 0,
                "success_count": 0,
                "failure_count": 0,
                "avg_generation_tokens_per_second": 0.0,
                "avg_prompt_tokens_per_second": 0.0,
                "avg_total_latency_ms": 0.0,
                "last_request_time": None,
                "last_success": True,
                "success_rate": 100.0,
            }
        successes = [r for r in records if r.success]
        failures = [r for r in records if not r.success]
        gen_tps = [r.generation_tokens_per_second for r in successes if r.generation_tokens_per_second > 0]
        prompt_tps = [r.prompt_tokens_per_second for r in successes if r.prompt_tokens_per_second > 0]
        latencies = [r.total_latency_ms for r in successes]
        total = len(records)
        return {
            "total_requests": total,
            "success_count": len(successes),
            "failure_count": len(failures),
            "success_rate": round(len(successes) / total * 100, 1) if total > 0 else 100.0,
            "avg_generation_tokens_per_second": round(sum(gen_tps) / len(gen_tps), 2) if gen_tps else 0.0,
            "avg_prompt_tokens_per_second": round(sum(prompt_tps) / len(prompt_tps), 2) if prompt_tps else 0.0,
            "avg_total_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
            "last_request_time": records[-1].timestamp if records else None,
            "last_success": records[-1].success if records else True,
        }


METRICS = MetricsStore()

# ──────────────────────────────────────────────────────────────────────────
# 5.  Utility functions
# ──────────────────────────────────────────────────────────────────────────

def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


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
# 6.  Authentication (AshatOS keys only — no admin)
# ──────────────────────────────────────────────────────────────────────────

class AuthError(Exception):
    def __init__(self, message: str = "Unauthorized") -> None:
        self.message = message
        super().__init__(message)


def _resolve_key(lane: str) -> str:
    if lane == "mainbrain":
        return _ASHAT_MAIN_KEY
    elif lane == "microbrain":
        return _ASHAT_MICRO_KEY
    return ""


def require_key(request: gr.Request, lane: str) -> None:
    """Raise AuthError if the request does not carry a valid X-Ashat-Key."""
    expected = _resolve_key(lane)
    if not expected:
        return
    supplied = (request.headers.get("x-ashat-key") or "").strip()
    if not hmac.compare_digest(supplied, expected):
        raise AuthError("Unauthorized")


def require_key_http(headers: dict[str, str], lane: str) -> None:
    """Raise AuthError from a plain dict of HTTP headers."""
    expected = _resolve_key(lane)
    if not expected:
        return
    supplied = (headers.get("x-ashat-key") or "").strip()
    if not hmac.compare_digest(supplied, expected):
        raise AuthError("Unauthorized")


# ──────────────────────────────────────────────────────────────────────────
# 7.  llama-server binary — download prebuilt from GitHub (no source build)
# ──────────────────────────────────────────────────────────────────────────

def _llama_cache_dir() -> Path:
    p = Path.home() / ".cache" / "ashatos" / "bin"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _find_existing_llama_server() -> str | None:
    which = shutil.which("llama-server")
    if which:
        return which
    return None


def _find_cached_llama_server(cache_dir: Path) -> str | None:
    for c in [cache_dir / "llama-server", cache_dir / "llama-server.exe"]:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def _extract_llama_archive(archive_path: str, extract_dir: str) -> str | None:
    dst = Path(extract_dir) / "llama-server"
    extracted: dict[str, bytes] = {}

    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            for n in zf.namelist():
                if n.endswith("/"):
                    continue
                extracted[Path(n).name] = zf.read(n)
    elif archive_path.endswith(".tar.gz") or archive_path.endswith(".tgz"):
        with tarfile.open(archive_path, "r:gz") as tf:
            for m in tf.getmembers():
                if m.isdir():
                    continue
                src = tf.extractfile(m)
                if src is not None:
                    extracted[Path(m.name).name] = src.read()
    else:
        return None

    for fname, content in extracted.items():
        target = Path(extract_dir) / fname
        target.write_bytes(content)
        target.chmod(0o755)

    candidate = Path(extract_dir) / "llama-server"
    if candidate.is_file():
        return str(candidate)
    for f in Path(extract_dir).iterdir():
        if f.is_file() and "llama-server" in f.name and os.access(f, os.X_OK):
            path = Path(extract_dir) / "llama-server"
            shutil.copy2(str(f), str(path))
            path.chmod(0o755)
            return str(path)
    return None


def _download_llama_server() -> str | None:
    cache = _llama_cache_dir()
    cached = _find_cached_llama_server(cache)
    if cached:
        return cached

    _log.info("llama: downloading prebuilt binary from GitHub ...")

    tag = LLAMA_SERVER_VERSION
    if not tag or tag == "latest":
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/ggerganov/llama.cpp/releases/latest",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                release = json.loads(resp.read().decode())
                tag = release.get("tag_name", "")
        except Exception as exc:
            _log.warning("llama: GitHub API error: %s", exc)
            return None

    if not tag:
        return None
    _log.info("llama: release tag: %s", tag)

    for suffix in [".tar.gz", ".zip"]:
        for os_name in ["ubuntu-x64", "linux-x64", "linux-amd64"]:
            fname = f"llama-{tag}-bin-{os_name}{suffix}"
            url = f"https://github.com/ggerganov/llama.cpp/releases/download/{tag}/{fname}"
            try:
                tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
                tmp_path = tmp.name
                tmp.close()
                try:
                    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        with open(tmp_path, "wb") as f:
                            f.write(resp.read())
                    result = _extract_llama_archive(tmp_path, str(cache))
                    os.unlink(tmp_path)
                    if result:
                        _log.info("llama: ready at %s", result)
                        return result
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
            except Exception:
                continue

    _log.error("llama: ALL DOWNLOAD STRATEGIES FAILED")
    return None


def ensure_llama_server() -> str | None:
    server_path = os.getenv("LLAMA_SERVER_PATH", "").strip()
    if server_path:
        p = Path(server_path)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)

    found = _find_existing_llama_server()
    if found:
        return found

    return _download_llama_server()


# ──────────────────────────────────────────────────────────────────────────
# 8.  Model download from HF Hub
# ──────────────────────────────────────────────────────────────────────────

def ensure_model(lane: str) -> str:
    cfg = LANES[lane]

    # Use env override if set
    env_key = f"{lane.upper()}_MODEL_PATH"
    env_path = os.getenv(env_key, "").strip()
    if env_path and os.path.isfile(env_path):
        cfg["model_path"] = env_path
        return env_path

    # Use cached path
    if cfg["model_path"] and os.path.isfile(cfg["model_path"]):
        return cfg["model_path"]

    # Download from HF Hub
    _log.info("%s: downloading %s/%s ...", lane, cfg["repo"], cfg["file"])
    path = hf_hub_download(
        repo_id=cfg["repo"],
        filename=cfg["file"],
        revision=MODEL_REVISION,
        token=HF_TOKEN,
    )
    cfg["model_path"] = path
    _log.info("%s: downloaded to %s", lane, path)
    return path


# ──────────────────────────────────────────────────────────────────────────
# 9.  llama-server lifecycle (per-request)
# ──────────────────────────────────────────────────────────────────────────

def _build_server_cmd(
    binary: str, model_path: str, ctx: int,
) -> list[str]:
    return [
        binary,
        "--host", "127.0.0.1",
        "--port", str(LLAMA_SERVER_PORT),
        "-m", model_path,
        "-c", str(ctx),
        "-t", str(N_THREADS),
        "-b", str(N_BATCH),
        "-ngl", "999",
    ]


def _wait_for_health(port: int, timeout: float = 30.0, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if resp.status_code < 500:
                return True
        except requests.RequestException:
            pass
        if is_port_open(port):
            return True
        time.sleep(interval)
    return False


def execute_lane_inner(lane: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Execute a single inference request against the given lane.
    No disk writes — all metrics are in-memory, llama-server output to DEVNULL.
    """
    request_id = payload.get("request_id", str(uuid.uuid4()))
    lane_cfg = LANES[lane]
    is_cold_start = not _downloaded_models.get(lane)
    t0 = time.perf_counter()

    try:
        model_path = ensure_model(lane)
        _downloaded_models[lane] = model_path

        # Start llama-server (stdout/stderr → DEVNULL, no disk logs)
        cmd = _build_server_cmd(
            str(_llama_bin_path or ""), model_path, lane_cfg["ctx"],
        )

        _log.info("%s: starting server on port %d ...", lane, LLAMA_SERVER_PORT)
        server_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _active_processes.append(server_proc)
        server_start_time = time.perf_counter()
        load_ms = round((server_start_time - t0) * 1000, 1)

        healthy = _wait_for_health(LLAMA_SERVER_PORT, timeout=30.0)
        health_time = time.perf_counter()
        server_start_ms = round((health_time - t0) * 1000, 1)

        if not healthy:
            _log.error("%s: server health check failed", lane)
            _terminate_process(server_proc, lane)
            try:
                _active_processes.remove(server_proc)
            except ValueError:
                pass
            METRICS.add_event(f"{lane}: server start failed")
            return {
                "ok": False, "request_id": request_id, "lane": lane,
                "error": {"code": "SERVER_START_FAILED",
                          "message": "llama-server did not become healthy",
                          "retryable": True},
            }

        backend = "cuda" if os.getenv("CUDA_VISIBLE_DEVICES") else "cpu"
        gpu_ok = (backend == "cuda")
        _log.info("%s: backend=%s gpu_offload=%s", lane, backend, gpu_ok)

        messages = payload.get("messages", [])
        if not messages:
            _terminate_process(server_proc, lane)
            try:
                _active_processes.remove(server_proc)
            except ValueError:
                pass
            return {
                "ok": False, "request_id": request_id, "lane": lane,
                "error": {"code": "INVALID_REQUEST",
                          "message": "No messages provided", "retryable": False},
            }

        completion_payload = {
            "model": lane_cfg["file"],
            "messages": messages,
            "max_tokens": min(
                int(payload.get("max_tokens", lane_cfg["max_tokens"])),
                lane_cfg["max_tokens"],
            ),
            "temperature": float(payload.get("temperature", 0.7)),
            "top_p": float(payload.get("top_p", 0.9)),
            "stream": False,
        }

        inference_start = time.perf_counter()
        resp = requests.post(
            f"http://127.0.0.1:{LLAMA_SERVER_PORT}/v1/chat/completions",
            json=completion_payload,
            timeout=120,
        )
        inference_end = time.perf_counter()
        total_latency_ms = round((inference_end - t0) * 1000, 1)

        if resp.status_code != 200:
            _terminate_process(server_proc, lane)
            try:
                _active_processes.remove(server_proc)
            except ValueError:
                pass
            return {
                "ok": False, "request_id": request_id, "lane": lane,
                "error": {"code": "INFERENCE_FAILED",
                          "message": f"llama-server returned HTTP {resp.status_code}",
                          "retryable": True},
            }

        data = resp.json()
        prompt_tokens = data.get("usage", {}).get("prompt_tokens", 0)
        completion_tokens = data.get("usage", {}).get("completion_tokens", 0)

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            text = ""

        gen_ms = max(1.0, total_latency_ms - server_start_ms)
        prompt_tps = round(prompt_tokens / (gen_ms / 1000), 2) if gen_ms > 0 else 0.0
        gen_tps = round(completion_tokens / (gen_ms / 1000), 2) if gen_ms > 0 else 0.0

        response: dict[str, Any] = {
            "id": f"ashat-{request_id[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": lane_cfg["file"],
            "lane": lane,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": data.get("choices", [{}])[0].get("finish_reason", "stop"),
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "performance": {
                "cold_start": is_cold_start,
                "server_start_ms": server_start_ms,
                "model_load_ms": load_ms,
                "total_latency_ms": total_latency_ms,
                "time_to_first_token_ms": None,
                "prompt_tokens_per_second": prompt_tps,
                "generation_tokens_per_second": gen_tps,
                "backend": backend,
                "gpu_offload_verified": gpu_ok,
            },
            "request_id": request_id,
            "ok": True,
        }

        # Record metrics (in-memory only)
        METRICS.record(MetricRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            lane=lane,
            success=True,
            cold_start=is_cold_start,
            server_start_ms=server_start_ms,
            model_load_ms=load_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_tokens_per_second=prompt_tps,
            generation_tokens_per_second=gen_tps,
            time_to_first_token_ms=None,
            total_latency_ms=total_latency_ms,
            backend=backend,
            gpu_offload_verified=gpu_ok,
        ))
        METRICS.add_event(f"{lane}: inference completed ({prompt_tokens}+{completion_tokens} tokens)")

        _terminate_process(server_proc, lane)
        try:
            _active_processes.remove(server_proc)
        except ValueError:
            pass

        return response

    except Exception as exc:
        _log.exception("%s: inference failed: %s", lane, exc)
        METRICS.record(MetricRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            lane=lane,
            success=False,
            cold_start=is_cold_start,
            total_latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            error_category="INTERNAL_ERROR",
        ))
        METRICS.add_event(f"{lane}: inference error — {exc}")
        return {
            "ok": False, "request_id": request_id, "lane": lane,
            "error": {"code": "INTERNAL_ERROR",
                      "message": str(exc)[:200], "retryable": True},
        }


@spaces.GPU(duration=LANES["microbrain"]["gpu_duration"])
def _execute_microbrain_gpu(payload: dict[str, Any]) -> dict[str, Any]:
    """MicroBrain inference with GPU allocation (called under @spaces.GPU)."""
    return execute_lane_inner("microbrain", payload)


@spaces.GPU(duration=LANES["mainbrain"]["gpu_duration"])
def _execute_mainbrain_gpu(payload: dict[str, Any]) -> dict[str, Any]:
    """MainBrain inference with GPU allocation (called under @spaces.GPU)."""
    return execute_lane_inner("mainbrain", payload)


def execute_lane(lane: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Thread-safe lane execution with GPU lifecycle."""
    with _inference_lock:
        if lane == "microbrain":
            return _execute_microbrain_gpu(payload)
        return _execute_mainbrain_gpu(payload)


# ──────────────────────────────────────────────────────────────────────────
# 10.  Request validation
# ──────────────────────────────────────────────────────────────────────────

def validate_request(body: dict[str, Any], lane: str) -> str | None:
    lane_cfg = LANES[lane]
    messages = body.get("messages", [])
    if not messages or not isinstance(messages, list):
        return "Missing or invalid 'messages' field"
    if len(messages) > lane_cfg["max_messages"]:
        return f"Too many messages (max {lane_cfg['max_messages']})"
    body_bytes = len(json.dumps(body))
    if body_bytes > lane_cfg["max_body_bytes"]:
        return f"Request body too large (max {lane_cfg['max_body_bytes']} bytes)"
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
# 11.  Gradio API functions
# ──────────────────────────────────────────────────────────────────────────

def microbrain_endpoint(
    payload_json: str,
    request: gr.Request,
) -> str:
    """Authenticated MicroBrain inference (Gradio API)."""
    try:
        require_key(request, "microbrain")
    except AuthError as e:
        return json.dumps({
            "ok": False, "error": {"code": "UNAUTHORIZED", "message": e.message, "retryable": False},
        })
    try:
        body = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
    except (json.JSONDecodeError, TypeError):
        return json.dumps({
            "ok": False, "error": {"code": "INVALID_REQUEST", "message": "Invalid JSON", "retryable": False},
        })
    err = validate_request(body, "microbrain")
    if err:
        return json.dumps({
            "ok": False, "error": {"code": "INVALID_REQUEST", "message": err, "retryable": False},
        })
    result = execute_lane("microbrain", body)
    return json.dumps(result)


def mainbrain_endpoint(
    payload_json: str,
    request: gr.Request,
) -> str:
    """Authenticated MainBrain inference (Gradio API)."""
    try:
        require_key(request, "mainbrain")
    except AuthError as e:
        return json.dumps({
            "ok": False, "error": {"code": "UNAUTHORIZED", "message": e.message, "retryable": False},
        })
    try:
        body = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
    except (json.JSONDecodeError, TypeError):
        return json.dumps({
            "ok": False, "error": {"code": "INVALID_REQUEST", "message": "Invalid JSON", "retryable": False},
        })
    err = validate_request(body, "mainbrain")
    if err:
        return json.dumps({
            "ok": False, "error": {"code": "INVALID_REQUEST", "message": err, "retryable": False},
        })
    result = execute_lane("mainbrain", body)
    return json.dumps(result)


def _public_status_json() -> str:
    s = _build_status()
    return json.dumps(s)


def _public_metrics_json() -> str:
    summaries = {
        "microbrain": METRICS.get_summary("microbrain"),
        "mainbrain": METRICS.get_summary("mainbrain"),
    }
    return json.dumps({
        "uptime_seconds": round(time.time() - _started_at, 1),
        "summaries": summaries,
        "recent_events": METRICS.get_events()[-20:],
        "total_events": len(METRICS.get_events()),
    })


def _build_status() -> dict[str, Any]:
    micro_avail = bool(_downloaded_models.get("microbrain"))
    main_avail = bool(_downloaded_models.get("mainbrain"))
    return {
        "uptime_seconds": round(time.time() - _started_at, 1),
        "llama_server": str(_llama_bin_path or "(not found)"),
        "lanes": {
            "microbrain": {
                "label": "MicroBrain",
                "model": LANES["microbrain"]["file"],
                "ctx": LANES["microbrain"]["ctx"],
                "available": micro_avail, "ready": micro_avail,
                **METRICS.get_summary("microbrain"),
            },
            "mainbrain": {
                "label": "MainBrain",
                "model": LANES["mainbrain"]["file"],
                "ctx": LANES["mainbrain"]["ctx"],
                "available": main_avail, "ready": main_avail,
                **METRICS.get_summary("mainbrain"),
            },
        },
        "all_ready": micro_avail and main_avail,
    }


# ──────────────────────────────────────────────────────────────────────────
# 12.  Dashboard HTML / refresh helpers
# ──────────────────────────────────────────────────────────────────────────

def _status_html() -> str:
    s = _build_status()
    lines = [
        '<div style="font-family: monospace; padding: 8px;">',
        f"<b>Uptime:</b> {s['uptime_seconds']:.0f}s &nbsp;|&nbsp; "
        f"<b>llama-server:</b> <code>{s['llama_server']}</code>",
    ]
    for key in ("mainbrain", "microbrain"):
        info = s["lanes"][key]
        emoji = "🟢" if info["ready"] else ("🔴" if not info["available"] else "🟡")
        last_req = info["last_request_time"] or "—"
        gen_tps = info["avg_generation_tokens_per_second"]
        latency = info["avg_total_latency_ms"]
        total = info["total_requests"]
        success_pct = info["success_rate"]
        lines.append(
            f'<div style="margin: 8px 0; padding: 8px; border: 1px solid #444; '
            f'border-radius: 6px; background: #1a1a2e;">'
            f'<b style="font-size: 1.1em;">{emoji} {info["label"]}</b><br>'
            f'<span style="color: #aaa;">Model:</span> {info["model"]} '
            f'<span style="color: #aaa;">Context:</span> {info["ctx"]}<br>'
            f'<span style="color: #aaa;">Requests:</span> {total} '
            f'<span style="color: #aaa;">Success:</span> {success_pct}%<br>'
            f'<span style="color: #aaa;">Avg gen tok/s:</span> {gen_tps} '
            f'<span style="color: #aaa;">Avg latency:</span> {latency}ms<br>'
            f'<span style="color: #aaa;">Last request:</span> {last_req}'
            f'</div>'
        )
    lines.append("</div>")
    return "\n".join(lines)


def _to_frame(records: list[MetricRecord]) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": r.timestamp,
            "generation_tokens_per_second": r.generation_tokens_per_second,
            "total_latency_ms": r.total_latency_ms,
            "prompt_tokens_per_second": r.prompt_tokens_per_second,
            "success": r.success,
        }
        for r in records[-50:]
    ]


# ──────────────────────────────────────────────────────────────────────────
# 13.  FastAPI app and Gradio dashboard
# ──────────────────────────────────────────────────────────────────────────

_fastapi_app = FastAPI(title="AshatOS Neural Host")


# === Custom HTTP routes (OpenAI-compatible API) ===

@_fastapi_app.post("/v1/chat/completions")
async def http_chat_completions(request: FastRequest) -> JSONResponse:
    """OpenAI-compatible chat completions endpoint (X-Ashat-Key auth)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={
            "error": {"message": "Invalid JSON body", "type": "invalid_request_error"},
        })

    model = (body.get("model") or "").lower()
    if "micro" in model or "350m" in model:
        lane = "microbrain"
    elif "main" in model or "1.2b" in model:
        lane = "mainbrain"
    else:
        lane = "mainbrain"

    headers = dict(request.headers)
    try:
        require_key_http(headers, lane)
    except AuthError as e:
        return JSONResponse(status_code=401, content={
            "error": {"message": e.message, "type": "authentication_error"},
        })

    err = validate_request(body, lane)
    if err:
        return JSONResponse(status_code=400, content={
            "error": {"message": err, "type": "invalid_request_error"},
        })

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, execute_lane, lane, body)

    if result.get("ok"):
        resp = {k: v for k, v in result.items() if k not in ("ok",)}
        return JSONResponse(content=resp)

    err_code = result.get("error", {}).get("code", "internal_error")
    err_msg = result.get("error", {}).get("message", "Unknown error")
    status = 503 if err_code in ("SERVER_START_FAILED", "INFERENCE_TIMEOUT") else \
             400 if err_code.startswith("INVALID") else 500
    return JSONResponse(status_code=status, content={
        "error": {"message": err_msg, "type": err_code.lower()},
    })


@_fastapi_app.get("/v1/models")
async def http_list_models() -> JSONResponse:
    return JSONResponse(content={
        "object": "list",
        "data": [
            {
                "id": LANES["mainbrain"]["file"],
                "object": "model",
                "created": int(_started_at),
                "owned_by": "ashatos",
            },
            {
                "id": LANES["microbrain"]["file"],
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
        "microbrain_ready": bool(_downloaded_models.get("microbrain")),
        "mainbrain_ready": bool(_downloaded_models.get("mainbrain")),
        "llama_server_available": _llama_bin_path is not None,
    })


@_fastapi_app.get("/api/public_status")
async def http_public_status() -> JSONResponse:
    return JSONResponse(content=_build_status())


@_fastapi_app.get("/api/public_metrics")
async def http_public_metrics() -> JSONResponse:
    return JSONResponse(content={
        "uptime_seconds": round(time.time() - _started_at, 1),
        "microbrain": METRICS.get_summary("microbrain"),
        "mainbrain": METRICS.get_summary("mainbrain"),
        "recent_events": METRICS.get_events()[-20:],
    })


# === Gradio dashboard (single page — live telemetry only) ===

JAVASCRIPT_REFRESH = f"""
<script>
setInterval(function() {{
    var btn = document.querySelector('#refresh-status-btn');
    if (btn) btn.click();
}}, {PUBLIC_REFRESH_SECONDS * 1000});
</script>
"""

with gr.Blocks(
    title="AshatOS Neural Host",
) as _demo:

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
            "🔄 Refresh Status", variant="secondary",
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
        | Model | `{LANES['microbrain']['file']}` | `{LANES['mainbrain']['file']}` |
        | Context | {LANES['microbrain']['ctx']} | {LANES['mainbrain']['ctx']} |
        | Max tokens | {LANES['microbrain']['max_tokens']} | {LANES['mainbrain']['max_tokens']} |
        | GPU duration | {LANES['microbrain']['gpu_duration']}s | {LANES['mainbrain']['gpu_duration']}s |

        ### Runtime

        - `LLAMA_SERVER_PORT`: {LLAMA_SERVER_PORT}
        - `N_THREADS`: {N_THREADS} | `N_BATCH`: {N_BATCH}
        - `PUBLIC_REFRESH_SECONDS`: {PUBLIC_REFRESH_SECONDS}
        - `QUEUE_LIMIT`: {QUEUE_LIMIT}
        """)

    # -- Refresh handlers --
    refresh_btn.click(
        fn=lambda: _status_html(),
        inputs=[],
        outputs=status_display,
        api_name="status",
        concurrency_limit=1,
    )

    def _refresh_metrics() -> tuple:
        all_m = METRICS.get_all_metrics()
        micro_frame = _to_frame(all_m.get("microbrain", []))
        main_frame = _to_frame(all_m.get("mainbrain", []))
        events = [{"Event": e} for e in METRICS.get_events()[-10:]]
        return (micro_frame, micro_frame, main_frame, main_frame, events)

    _demo.load(
        fn=_refresh_metrics,
        inputs=[],
        outputs=[micro_gen_plot, micro_latency_plot, main_gen_plot, main_latency_plot, events_display],
        concurrency_limit=1,
    )

    # -- Private Gradio API endpoints (AshatOS communication only) --
    _micro_input = gr.Textbox(visible=False, value="{}", label="microbrain_payload")
    _micro_trigger = gr.Button(visible=False, elem_id="_micro_trigger")
    _micro_trigger.click(
        fn=microbrain_endpoint,
        inputs=[_micro_input],
        outputs=[gr.Textbox(visible=False)],
        api_name="microbrain",
        concurrency_limit=1,
    )

    _main_input = gr.Textbox(visible=False, value="{}", label="mainbrain_payload")
    _main_trigger = gr.Button(visible=False, elem_id="_main_trigger")
    _main_trigger.click(
        fn=mainbrain_endpoint,
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


# ──────────────────────────────────────────────────────────────────────────
# 14.  Startup
# ──────────────────────────────────────────────────────────────────────────

def startup() -> None:
    global _llama_bin_path
    _log.info("=" * 60)
    _log.info("AshatOS Neural I/O Host — Dual-Lane Inference")
    _log.info("=" * 60)

    # Ensure llama-server binary
    _llama_bin_path = ensure_llama_server()
    if _llama_bin_path:
        _log.info("llama-server binary: %s", _llama_bin_path)
    else:
        _log.warning("llama-server binary not available — degraded mode")

    # Models will download on first inference (ensures up-to-date on HF Hub)


startup()


# ──────────────────────────────────────────────────────────────────────────
# 15.  Mount Gradio on FastAPI and launch
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
        uvicorn.run(app, host="0.0.0.0", port=7860)
