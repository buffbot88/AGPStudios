#!/usr/bin/env python3
"""
AshatOS Dual-Lane ZeroGPU Inference Host

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
import tempfile
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

# ZeroGPU compatibility — supports both @spaces_gpu and @spaces_gpu(duration=N)
try:
    from spaces import GPU as _spaces_gpu
    def spaces_gpu(f=None, **kwargs):
        if f is not None:
            return _spaces_gpu(f)
        return _spaces_gpu(**kwargs)
except ImportError:
    def spaces_gpu(f=None, **kwargs):  # type: ignore[no-redef]
        if f is not None:
            return f
        return lambda f: f

# ──────────────────────────────────────────────────────────────────────────
# 1.  Logging
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

# Models
MAIN_MODEL_REPO = os.getenv("MAIN_MODEL_REPO", "RipBuffy/LFM2.5-Q6_K")
MAIN_MODEL_FILE = os.getenv("MAIN_MODEL_FILE", "LFM2.5-1.2B-Instruct-Q6_K.gguf")
MICRO_MODEL_REPO = os.getenv("MICRO_MODEL_REPO", "RipBuffy/LFM2.5-Q6_K")
MICRO_MODEL_FILE = os.getenv("MICRO_MODEL_FILE", "LFM2.5-350M-Q6_K.gguf")
MODEL_REVISION = os.getenv("MODEL_REVISION", "main")
HF_TOKEN: str | None = os.getenv("HF_TOKEN") or None

# Runtime
INTERNAL_PORT = int(os.getenv("INTERNAL_PORT", "18080"))
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
LLAMA_SERVER_VERSION = os.getenv("LLAMA_SERVER_VERSION", "")

# Paths
RUNTIME_DIR = Path("./.runtime")
CACHE_BIN_DIR = RUNTIME_DIR / "bin"
LOGS_DIR = Path("./logs")
LLAMA_CPP_SRC = RUNTIME_DIR / "llama.cpp"

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

# Authentication keys (Space secrets)
_ASHAT_MICRO_KEY: str = os.getenv("ASHAT_MICROBRAIN_KEY", "")
_ASHAT_MAIN_KEY: str = os.getenv("ASHAT_MAINBRAIN_KEY", "")
_ASHAT_ADMIN_KEY: str = os.getenv("ASHAT_ADMIN_KEY", "")

# Benchmark prompts
_BENCHMARK_PROMPTS: dict[str, str] = {
    "microbrain": "Write exactly three concise facts about the Moon.",
    "mainbrain": (
        "Analyze a simplified software deadlock scenario:\n"
        "Thread A holds lock X and waits for lock Y.\n"
        "Thread B holds lock Y and waits for lock X.\n"
        "Identify the likely cause and resolution."
    ),
}

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
# 4.  Metrics store (thread-safe, rolling deque)
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
    """Thread-safe in-memory rolling metrics store."""

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


def _tail_log(path: Path, n: int = 30) -> str:
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return "(unreadable)"


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


def _print_key_gen_help() -> None:
    _log.info("=" * 60)
    _log.info("Key Generation (run on your local machine):")
    _log.info("  python -c \"import secrets; print('MICRO:', secrets.token_urlsafe(48))\"")
    _log.info("  python -c \"import secrets; print('MAIN: ', secrets.token_urlsafe(48))\"")
    _log.info("  python -c \"import secrets; print('ADMIN:', secrets.token_urlsafe(48))\"")
    _log.info("=" * 60)


# ──────────────────────────────────────────────────────────────────────────
# 6.  Authentication
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
        return  # no key configured → open (discouraged for prod)
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
# 7.  llama-server install / detect
# ──────────────────────────────────────────────────────────────────────────

def _find_existing_llama_server() -> str | None:
    which = shutil.which("llama-server")
    if which:
        _log.info("install: found llama-server on PATH: %s", which)
        return which
    candidates = [
        "./llama-server", "./llama-server.exe",
        "./bin/llama-server", "/usr/local/bin/llama-server",
    ]
    for c in candidates:
        p = Path(c)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve())
    return None


def _find_cached_llama_server() -> str | None:
    candidates = [CACHE_BIN_DIR / "llama-server", CACHE_BIN_DIR / "llama-server.exe"]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def _extract_archive(archive_path: str, extract_dir: str) -> str | None:
    """Extract all files flat into extract_dir and return the binary path."""
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
        _log.warning("install: unsupported archive format: %s", archive_path)
        return None

    if not extracted:
        return None

    _log.info("install: extracted %d files from archive", len(extracted))
    for fname, content in extracted.items():
        target = Path(extract_dir) / fname
        target.write_bytes(content)
        target.chmod(0o755)

    if dst.is_file():
        return str(dst)
    if "llama-server" in extracted:
        src_path = Path(extract_dir) / "llama-server"
        if src_path != dst:
            shutil.move(str(src_path), str(dst))
        return str(dst)
    for f in Path(extract_dir).iterdir():
        if f.is_file() and "llama-server" in f.name and os.access(f, os.X_OK):
            if f != dst:
                shutil.copy2(str(f), str(dst))
                dst.chmod(0o755)
            return str(dst)
    return None


def _get_latest_release_tag() -> str | None:
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/ggerganov/llama.cpp/releases/latest",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            release = json.loads(resp.read().decode())
            return release.get("tag_name", "")
    except Exception as exc:
        _log.warning("install: GitHub API error: %s", exc)
        return None


def _download_prebuilt_llama_server() -> str | None:
    _log.info("install: downloading prebuilt llama-server from GitHub ...")
    dst = CACHE_BIN_DIR / "llama-server"
    if dst.is_file():
        return str(dst)

    tag = LLAMA_SERVER_VERSION or _get_latest_release_tag()
    if not tag:
        return None
    _log.info("install: llama.cpp release: %s", tag)

    candidates: list[tuple[str, str]] = []
    for suffix in [".tar.gz", ".zip"]:
        for os_name in ["ubuntu-x64", "linux-x64", "linux-amd64"]:
            fname = f"llama-{tag}-bin-{os_name}{suffix}"
            url = f"https://github.com/ggerganov/llama.cpp/releases/download/{tag}/{fname}"
            candidates.append((fname, url))

    for fname, url in candidates:
        suffix = ".tar.gz" if fname.endswith(".tar.gz") else ".zip"
        try:
            CACHE_BIN_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = tmp.name
                req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    tmp.write(resp.read())
            result = _extract_archive(tmp_path, str(CACHE_BIN_DIR))
            os.unlink(tmp_path)
            if result:
                _log.info("install: llama-server ready at %s", result)
                return result
        except Exception:
            continue
    return None


def _build_llama_server_from_source() -> str | None:
    _log.info("install: building llama-server from source (CPU fallback) ...")
    LLAMA_CPP_SRC.mkdir(parents=True, exist_ok=True)

    if not (LLAMA_CPP_SRC / "CMakeLists.txt").is_file():
        result = subprocess.run(
            ["git", "clone", "--depth", "1",
             "https://github.com/ggerganov/llama.cpp.git", str(LLAMA_CPP_SRC)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            _log.warning("install: git clone failed: %s", result.stderr[:500])
            return None

    build_dir = LLAMA_CPP_SRC / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["cmake", "-B", "build", "-DGGML_CUDA=OFF", "-DGGML_NATIVE=OFF"],
        capture_output=True, text=True, timeout=120, cwd=str(LLAMA_CPP_SRC),
    )
    if result.returncode != 0:
        _log.warning("install: cmake configure failed: %s", result.stderr[:500])
        return None

    result = subprocess.run(
        ["cmake", "--build", "build", "--config", "Release", "-j"],
        capture_output=True, text=True, timeout=600, cwd=str(LLAMA_CPP_SRC),
    )
    if result.returncode != 0:
        _log.warning("install: cmake build failed: %s", result.stderr[:500])
        return None

    for c in [
        build_dir / "bin" / "llama-server",
        build_dir / "bin" / "Release" / "llama-server",
    ]:
        if c.is_file():
            CACHE_BIN_DIR.mkdir(parents=True, exist_ok=True)
            cached = CACHE_BIN_DIR / c.name
            shutil.copy2(str(c), str(cached))
            cached.chmod(0o755)
            return str(cached)
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
    found = _find_cached_llama_server()
    if found:
        return found

    if os.getenv("AUTO_BUILD_LLAMA_SERVER", "1") not in ("1", "true", "yes"):
        return None

    found = _download_prebuilt_llama_server()
    if found:
        return found
    found = _build_llama_server_from_source()
    if found:
        return found

    _log.error("install: ALL INSTALL STRATEGIES FAILED")
    return None


# ──────────────────────────────────────────────────────────────────────────
# 8.  Model download
# ──────────────────────────────────────────────────────────────────────────

def ensure_model(lane: str) -> str:
    cfg = LANES[lane]
    env_key = f"{lane.upper()}_MODEL_PATH"
    local_path = os.getenv(env_key, "").strip()
    if local_path and os.path.isfile(local_path):
        _log.info("%s: using local path %s", lane, local_path)
        return local_path

    if cfg["model_path"] and os.path.isfile(cfg["model_path"]):
        return cfg["model_path"]

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
    binary: str, model_path: str, port: int, ctx: int,
) -> list[str]:
    return [
        binary,
        "--host", "127.0.0.1",
        "--port", str(port),
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


def _verify_gpu_from_logs(err_log: Path) -> tuple[str, bool]:
    tail = _tail_log(err_log)
    if not tail:
        return "unknown", False
    has_cuda = any(marker in tail.lower() for marker in
                   ["ggml_cuda_init", "found cuda device", "cuda0", "offloaded layers"])
    if has_cuda:
        return "cuda", True
    return "cpu", False


def execute_lane_inner(lane: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Execute a single inference request against the given lane.
    This is the inner function called from the @spaces.GPU wrapper.
    """
    request_id = payload.get("request_id", str(uuid.uuid4()))
    lane_cfg = LANES[lane]
    is_cold_start = not _downloaded_models.get(lane)
    t0 = time.perf_counter()  # initialized before try

    try:
        model_path = ensure_model(lane)
        _downloaded_models[lane] = model_path

        # Start llama-server
        cmd = _build_server_cmd(
            str(_llama_bin_path or ""), model_path,
            INTERNAL_PORT, lane_cfg["ctx"],
        )
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        err_log = LOGS_DIR / f"{lane}.err.log"
        out_log = LOGS_DIR / f"{lane}.out.log"

        _log.info("%s: starting server on port %d ...", lane, INTERNAL_PORT)
        server_proc = subprocess.Popen(
            cmd,
            stdout=open(out_log, "w", encoding="utf-8"),
            stderr=open(err_log, "w", encoding="utf-8"),
        )
        _active_processes.append(server_proc)
        server_start_time = time.perf_counter()
        load_ms = round((server_start_time - t0) * 1000, 1)

        # Wait for health
        healthy = _wait_for_health(INTERNAL_PORT, timeout=30.0)
        health_time = time.perf_counter()
        server_start_ms = round((health_time - t0) * 1000, 1)

        if not healthy:
            err_tail = _tail_log(err_log)
            _log.error("%s: server health check failed", lane)
            _terminate_process(server_proc, lane)
            METRICS.add_event(f"{lane}: server start failed")
            # Try to remove from active processes
            try:
                _active_processes.remove(server_proc)
            except ValueError:
                pass
            return {
                "ok": False, "request_id": request_id, "lane": lane,
                "error": {"code": "SERVER_START_FAILED",
                          "message": f"llama-server did not become healthy",
                          "retryable": True},
            }

        # Verify GPU offload
        backend, gpu_ok = _verify_gpu_from_logs(err_log)
        _log.info("%s: backend=%s gpu_offload=%s", lane, backend, gpu_ok)

        # Build messages
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

        # Send completion request
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
            f"http://127.0.0.1:{INTERNAL_PORT}/v1/chat/completions",
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

        # Build OpenAI-compatible response
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

        # Record metrics
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

        # Terminate server before returning
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


@spaces_gpu(duration=LANES["microbrain"]["gpu_duration"])
def _execute_microbrain_gpu(payload: dict[str, Any]) -> dict[str, Any]:
    """MicroBrain inference with GPU allocation (called under @spaces.GPU)."""
    return execute_lane_inner("microbrain", payload)


@spaces_gpu(duration=LANES["mainbrain"]["gpu_duration"])
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


def _admin_benchmark_endpoint(lane: str, request: gr.Request) -> str:
    try:
        expected = _ASHAT_ADMIN_KEY
        if not expected:
            raise AuthError("Admin endpoint not configured")
        supplied = (request.headers.get("x-ashat-key") or "").strip()
        if not hmac.compare_digest(supplied, expected):
            raise AuthError("Unauthorized")
    except AuthError as e:
        return json.dumps({"ok": False, "error": {"code": "UNAUTHORIZED", "message": e.message}})

    target = lane.strip().lower()
    if target not in ("microbrain", "mainbrain", "both"):
        return json.dumps({"ok": False, "error": "Specify 'microbrain', 'mainbrain', or 'both'"})

    results: dict[str, dict[str, Any]] = {}
    if target in ("microbrain", "both"):
        payload = {
            "request_id": f"bench-{uuid.uuid4()}",
            "messages": [{"role": "user", "content": _BENCHMARK_PROMPTS["microbrain"]}],
            "max_tokens": 64, "temperature": 0.1,
        }
        results["microbrain"] = execute_lane("microbrain", payload)
    if target in ("mainbrain", "both"):
        payload = {
            "request_id": f"bench-{uuid.uuid4()}",
            "messages": [{"role": "user", "content": _BENCHMARK_PROMPTS["mainbrain"]}],
            "max_tokens": 96, "temperature": 0.1,
        }
        results["mainbrain"] = execute_lane("mainbrain", payload)
    return json.dumps({"ok": True, "results": results})


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

# Create the FastAPI app first (for custom routes)
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

    # Run blocking inference in executor (don't block event loop)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, execute_lane, lane, body)

    if result.get("ok"):
        # Return standard OpenAI-compatible response, keeping extra metadata
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


# === Gradio dashboard ===

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

    gr.Markdown("## Recent Health Events")
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
        | Repository | `{LANES['microbrain']['repo']}` | `{LANES['mainbrain']['repo']}` |
        | Context | {LANES['microbrain']['ctx']} | {LANES['mainbrain']['ctx']} |
        | Max tokens | {LANES['microbrain']['max_tokens']} | {LANES['mainbrain']['max_tokens']} |
        | GPU duration | {LANES['microbrain']['gpu_duration']}s | {LANES['mainbrain']['gpu_duration']}s |

        ### Runtime

        - `INTERNAL_PORT`: {INTERNAL_PORT}
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

    # -- Register private Gradio API endpoints (hidden triggers inside Blocks) --
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

    _benchmark_input = gr.Textbox(visible=False, value="both", label="benchmark_lane")
    _benchmark_trigger = gr.Button(visible=False, elem_id="_benchmark_trigger")
    _benchmark_trigger.click(
        fn=_admin_benchmark_endpoint,
        inputs=[_benchmark_input],
        outputs=[gr.Textbox(visible=False)],
        api_name="admin_benchmark",
        concurrency_limit=1,
    )


# ──────────────────────────────────────────────────────────────────────────
# 15.  Startup
# ──────────────────────────────────────────────────────────────────────────

def startup() -> None:
    global _llama_bin_path
    _log.info("=" * 60)
    _log.info("AshatOS Dual-Lane ZeroGPU Inference Host")
    _log.info("=" * 60)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _print_key_gen_help()

    _llama_bin_path = ensure_llama_server()
    if _llama_bin_path:
        _log.info("llama-server binary: %s", _llama_bin_path)
    else:
        _log.warning("llama-server binary not available — degraded mode")

    def _dl(lane: str) -> None:
        try:
            ensure_model(lane)
            _log.info("%s: model cached at startup", lane)
            METRICS.add_event(f"{lane}: model cached")
        except Exception as exc:
            _log.warning("%s: model download failed: %s", lane, exc)
            METRICS.add_event(f"{lane}: model download failed")

    t1 = threading.Thread(target=_dl, args=("microbrain",), daemon=True)
    t2 = threading.Thread(target=_dl, args=("mainbrain",), daemon=True)
    t1.start()
    t2.start()


startup()


# ──────────────────────────────────────────────────────────────────────────
# 16.  Mount Gradio on FastAPI and launch
# ──────────────────────────────────────────────────────────────────────────

# Queue configuration
_demo.queue(default_concurrency_limit=1, max_size=QUEUE_LIMIT)

# Mount Gradio at the root of our FastAPI app
app = gr.mount_gradio_app(
    _fastapi_app, _demo, path="/",
    theme=gr.themes.Soft(),
    head=JAVASCRIPT_REFRESH,
)

# Note: On HF Spaces, the runtime auto-serves the `app` FastAPI+Gradio object on port 7860.
# For local development, run:  uvicorn app:app --host 0.0.0.0 --port 7860
