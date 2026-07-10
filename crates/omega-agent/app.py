#!/usr/bin/env python3
"""
AshatOS Dual llama-server Host

Launches two llama-server subprocesses (MainBrain / MicroBrain) behind a
Gradio UI.  No runtime dependency on `llama-cpp-python` — inference happens
through the local HTTP servers.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import gradio as gr
import requests
from huggingface_hub import hf_hub_download

# ZeroGPU compatibility: the Hugging Face Spaces zeroGPU runtime requires
# a @spaces.GPU-decorated function to be present and called during startup.
# If `spaces` isn't available (local dev, CPU Spaces), this is a no-op.
try:
    from spaces import GPU as spaces_gpu
except ImportError:
    spaces_gpu = lambda f: f  # no-op fallback decorator

# ---------------------------------------------------------------------------
# 1.  Model definitions (preserved from original app.py)
# ---------------------------------------------------------------------------

# Fast-lane (MainBrain)
FAST_MODEL_REPO = os.getenv("FAST_MODEL_REPO", "RipBuffy/LFM2.5-Q6_K").strip()
FAST_MODEL_FILE = os.getenv("FAST_MODEL_FILE", "LFM2.5-350M-Q6_K.gguf").strip()

# Slow-lane (MicroBrain)
SLOW_MODEL_REPO = os.getenv("SLOW_MODEL_REPO", "RipBuffy/LFM2.5-Q6_K").strip()
SLOW_MODEL_FILE = os.getenv("SLOW_MODEL_FILE", "LFM2.5-1.2B-Instruct-Q6_K.gguf").strip()

MODEL_REVISION = os.getenv("MODEL_REVISION", "main").strip()
HF_TOKEN = os.getenv("HF_TOKEN") or None

# Original context sizes
N_CTX_FAST = int(os.getenv("N_CTX_FAST", "1024"))
N_CTX_SLOW = int(os.getenv("N_CTX_SLOW", "1536"))

# ---------------------------------------------------------------------------
# 2.  Normalised model registry
# ---------------------------------------------------------------------------

MODELS: dict[str, dict[str, Any]] = {
    # MainBrain = big/reasoning model (1.2B Instruct)
    "MainBrain": {
        "repo": SLOW_MODEL_REPO,
        "file": SLOW_MODEL_FILE,
        "port": int(os.getenv("MAINBRAIN_PORT", "18080")),
        "ctx": int(os.getenv("MAINBRAIN_CTX", str(N_CTX_SLOW))),
        "local_path": os.getenv("MAINBRAIN_MODEL_PATH", "").strip() or None,
        "system_prompt": os.getenv(
            "SLOW_SYSTEM_PROMPT",
            "You are Ashat's careful reasoning lane. Think carefully and return a clear final answer.",
        ),
    },
    # MicroBrain = small/fast model (350M)
    "MicroBrain": {
        "repo": FAST_MODEL_REPO,
        "file": FAST_MODEL_FILE,
        "port": int(os.getenv("MICROBRAIN_PORT", "18081")),
        "ctx": int(os.getenv("MICROBRAIN_CTX", str(N_CTX_FAST))),
        "local_path": os.getenv("MICROBRAIN_MODEL_PATH", "").strip() or None,
        "system_prompt": os.getenv(
            "FAST_SYSTEM_PROMPT",
            "You are Ashat's fast conversational lane. Be concise, natural, and helpful.",
        ),
    },
}

# ---------------------------------------------------------------------------
# 3.  Runtime constants
# ---------------------------------------------------------------------------

RUNTIME_DIR = Path("./.runtime")
CACHE_BIN_DIR = RUNTIME_DIR / "bin"
LLAMA_CPP_SRC = RUNTIME_DIR / "llama.cpp"
LOGS_DIR = Path("./logs")

LLAMA_THREADS = int(os.getenv("LLAMA_THREADS", "2"))
LLAMA_BATCH_SIZE = int(os.getenv("LLAMA_BATCH_SIZE", "128"))
AUTO_BUILD = os.getenv("AUTO_BUILD_LLAMA_SERVER", "1") in ("1", "true", "yes")

# Override path to llama-server binary (skip auto-detect)
LLAMA_SERVER_PATH = os.getenv("LLAMA_SERVER_PATH", "").strip() or None

_started_at = time.time()
_server_processes: dict[str, subprocess.Popen | None] = {
    "MainBrain": None,
    "MicroBrain": None,
}
_server_ready: dict[str, bool] = {
    "MainBrain": False,
    "MicroBrain": False,
}
_server_errors: dict[str, str] = {
    "MainBrain": "",
    "MicroBrain": "",
}
_downloaded_models: dict[str, str] = {}  # lane -> local GGUF path

# ---------------------------------------------------------------------------
# 4.  Logging helpers
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
_log = logging.getLogger("ashatos")


def _log_install(msg: str) -> None:
    """Write to both the install log and the application log."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOGS_DIR / "llama_install.log", "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    _log.info("install: %s", msg)


def _log_install_fmt(fmt: str, *args: object) -> None:
    """Format and write an install log message."""
    _log_install(fmt % args if args else fmt)


# ---------------------------------------------------------------------------
# 5.  Utility functions
# ---------------------------------------------------------------------------

def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Check whether *something* is listening on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        return sock.connect_ex((host, port)) == 0


def _tail_log(path: Path, n: int = 20) -> str:
    """Return the last *n* lines of a file, or an empty string."""
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return "(unreadable)"


# ---------------------------------------------------------------------------
# 6.  Download GGUF models  (preserves original hf_hub_download logic)
# ---------------------------------------------------------------------------

def ensure_model(lane: str, cfg: dict[str, Any]) -> str:
    """Return a local path to the GGUF file for *lane*, downloading if needed."""
    if cfg["local_path"]:
        p = cfg["local_path"]
        if os.path.isfile(p):
            _log.info("%s: using local path %s", lane, p)
            return p
        _log.warning("%s: %s set but not found (%s)", lane,
                     f"{lane.upper()}_MODEL_PATH", p)

    # Download from Hugging Face Hub (same as original app.py)
    _log.info("%s: downloading %s/%s ...", lane, cfg["repo"], cfg["file"])
    path = hf_hub_download(
        repo_id=cfg["repo"],
        filename=cfg["file"],
        revision=MODEL_REVISION,
        token=HF_TOKEN,
    )
    _log.info("%s: downloaded to %s", lane, path)
    return path


# ---------------------------------------------------------------------------
# 7.  llama-server install / detect
# ---------------------------------------------------------------------------

def _find_existing_llama_server() -> str | None:
    """Check common locations and PATH for an existing llama-server binary."""
    which = shutil.which("llama-server")
    if which:
        _log_install_fmt("found llama-server on PATH: %s", which)
        return which

    candidates = [
        "./llama-server",
        "./llama-server.exe",
        "./bin/llama-server",
        "/usr/local/bin/llama-server",
    ]
    for c in candidates:
        p = Path(c)
        if p.is_file() and os.access(p, os.X_OK):
            _log_install_fmt("found llama-server at %s", p.resolve())
            return str(p.resolve())
    return None


def _find_cached_llama_server() -> str | None:
    """Check the local runtime cache directory."""
    candidates = [
        CACHE_BIN_DIR / "llama-server",
        CACHE_BIN_DIR / "llama-server.exe",
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            _log_install_fmt("found cached llama-server at %s", c)
            return str(c)
    return None


def _find_llama_server_member(names: list[str]) -> str | None:
    """From a list of archive member paths, pick the best `llama-server`
    candidate.  Priority:
    1. Basename is exactly ``llama-server``
    2. Basename is exactly ``llama-server.exe``
    3. Any member containing ``llama-server`` that is NOT a ``.so`` library
    4. Any member containing ``llama-server`` (last resort)
    """
    candidates: list[tuple[int, str]] = []

    for n in names:
        fname = Path(n).name  # last path component
        if fname == "llama-server":
            candidates.append((1, n))
        elif fname == "llama-server.exe":
            candidates.append((2, n))
        elif "llama-server" in n and not n.endswith(".so"):
            candidates.append((3, n))
        elif "llama-server" in n:
            candidates.append((4, n))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _extract_archive(archive_path: str, extract_dir: str) -> str | None:
    """Extract everything from the prebuilt archive into *extract_dir* flatly,
    then return the path to the ``llama-server`` executable.  We extract *all*
    files so that shared libraries (``libllama-server*.so``) end up alongside
    the binary and can be loaded at runtime."""
    dst = Path(extract_dir) / "llama-server"
    extracted: dict[str, bytes] = {}  # filename -> content

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
        _log_install_fmt("unsupported archive format: %s", archive_path)
        return None

    if not extracted:
        _log_install("empty archive — nothing extracted")
        return None

    _log_install_fmt("extracted %d files from archive", len(extracted))

    # Write all files flat into extract_dir, make everything executable so
    # that the dynamic linker can mmap(PROT_EXEC) .so files.
    for fname, content in extracted.items():
        target = Path(extract_dir) / fname
        target.write_bytes(content)
        target.chmod(0o755)

    # Ensure the canonical binary path exists
    if dst.is_file():
        return str(dst)

    # If the binary was named differently, rename it
    if "llama-server" in extracted:
        src_path = Path(extract_dir) / "llama-server"
        if src_path != dst:
            shutil.move(str(src_path), str(dst))
        return str(dst)

    _log_install("llama-server binary not found among extracted files")
    return None


def _is_linux_x86_64_asset(name: str) -> bool:
    """Return True if *name* looks like a plain Linux x86_64 prebuilt archive
    (excluding specialized builds like OpenVINO, ROCm, SYCL, Vulkan)."""
    n = name.lower()
    # Exclude specialized / GPU-specific builds
    excluded = {"openvino", "rocm", "sycl", "vulkan", "hip", "cuda"}
    if any(x in n for x in excluded):
        return False
    # Match: ubuntu-x64, linux-x64, linux-amd64, linux-x86_64
    if "ubuntu" in n and "x64" in n:
        return True
    if "linux" in n and ("amd64" in n or "x86_64" in n or "x64" in n):
        return True
    return False


def _download_prebuilt_llama_server() -> str | None:
    """Download a prebuilt llama-server from GitHub releases."""
    _log_install("attempting to download prebuilt llama-server from GitHub ...")

    dst = CACHE_BIN_DIR / "llama-server"
    if dst.is_file():
        _log_install_fmt("prebuilt binary already cached at %s", dst)
        return str(dst)

    # Discover the latest release tag via the GitHub API
    api_url = "https://api.github.com/repos/ggerganov/llama.cpp/releases/latest"
    try:
        req = urllib.request.Request(api_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            release = json.loads(resp.read().decode())
    except Exception as exc:
        _log_install_fmt("GitHub API error (latest release): %s", exc)
        return None

    tag = release.get("tag_name", "")
    if not tag:
        _log_install("no tag_name in latest release response")
        return None

    _log_install_fmt("latest llama.cpp release: %s", tag)

    # Look for a Linux x86_64 prebuilt archive (.zip or .tar.gz)
    assets = release.get("assets", [])
    candidates: list[tuple[str, str]] = []  # (name, url)

    for asset in assets:
        name: str = asset.get("name", "")
        if not (name.endswith(".zip") or name.endswith(".tar.gz") or name.endswith(".tgz")):
            continue
        if _is_linux_x86_64_asset(name):
            candidates.append((name, asset["browser_download_url"]))
            _log_install_fmt("found prebuilt asset: %s", name)

    if not candidates:
        _log_install_fmt(
            "no Linux x86_64 prebuilt archive found in release %s — "
            "listing all assets for debugging:", tag
        )
        for asset in assets:
            _log_install_fmt("  available asset: %s", asset.get("name", "?"))
        # Fallback URL patterns for both zip and tar.gz
        fallback_names = [
            f"llama-{tag}-bin-ubuntu-x64.zip",
            f"llama-{tag}-bin-ubuntu-x64.tar.gz",
            f"llama-{tag}-bin-linux-x64.zip",
            f"llama-{tag}-bin-linux-amd64.zip",
            f"llama-{tag}-bin-linux-x64.tar.gz",
        ]
        for fname in fallback_names:
            url = f"https://github.com/ggerganov/llama.cpp/releases/download/{tag}/{fname}"
            candidates.append((fname, url))

    # Try each candidate URL until one succeeds
    for asset_name, asset_url in candidates:
        suffix = ".tar.gz" if (asset_name.endswith(".tar.gz") or asset_name.endswith(".tgz")) else ".zip"
        _log_install_fmt("trying: %s", asset_url)

        try:
            CACHE_BIN_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = tmp.name
                req = urllib.request.Request(
                    asset_url, headers={"Accept": "application/octet-stream"}
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    tmp.write(resp.read())

            result = _extract_archive(tmp_path, str(CACHE_BIN_DIR))
            os.unlink(tmp_path)

            if result:
                _log_install_fmt("prebuilt llama-server ready at %s", result)
                return result
        except Exception as exc:
            _log_install_fmt("  failed: %s", exc)
            continue

    return None


def _build_llama_server_from_source() -> str | None:
    """Clone llama.cpp and build llama-server from source (CPU-only build)."""
    _log_install("building llama-server from source ...")

    LLAMA_CPP_SRC.mkdir(parents=True, exist_ok=True)

    # Check if already cloned
    if not (LLAMA_CPP_SRC / "CMakeLists.txt").is_file():
        _log_install_fmt("cloning llama.cpp into %s ...", LLAMA_CPP_SRC)
        result = subprocess.run(
            ["git", "clone", "--depth", "1",
             "https://github.com/ggerganov/llama.cpp.git",
             str(LLAMA_CPP_SRC)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            _log_install_fmt("git clone failed:\n%s", result.stderr[:2000])
            return None
        _log_install("clone successful")
    else:
        _log_install_fmt("llama.cpp already cloned at %s", LLAMA_CPP_SRC)

    # CMake configure
    build_dir = LLAMA_CPP_SRC / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    _log_install("cmake -B build ...")
    result = subprocess.run(
        ["cmake", "-B", "build", "-DGGML_CUDA=OFF", "-DGGML_NATIVE=OFF"],
        capture_output=True, text=True, timeout=120,
        cwd=str(LLAMA_CPP_SRC),
    )
    if result.returncode != 0:
        _log_install_fmt("cmake configure failed:\n%s", result.stderr[:2000])
        return None
    _log_install("cmake configure OK")

    # CMake build
    _log_install("cmake --build build --config Release -j ...")
    result = subprocess.run(
        ["cmake", "--build", "build", "--config", "Release", "-j"],
        capture_output=True, text=True, timeout=600,
        cwd=str(LLAMA_CPP_SRC),
    )
    if result.returncode != 0:
        _log_install_fmt("cmake build failed:\n%s", result.stderr[:2000])
        return None
    _log_install("cmake build OK")

    # Locate the built binary
    candidates = [
        build_dir / "bin" / "llama-server",
        build_dir / "bin" / "Release" / "llama-server",
        build_dir / "bin" / "llama-server.exe",
        build_dir / "bin" / "Release" / "llama-server.exe",
    ]
    for c in candidates:
        if c.is_file():
            _log_install_fmt("built llama-server found at %s", c)
            CACHE_BIN_DIR.mkdir(parents=True, exist_ok=True)
            cached = CACHE_BIN_DIR / c.name
            shutil.copy2(str(c), str(cached))
            cached.chmod(cached.stat().st_mode | 0o111)
            return str(cached)

    _log_install("build succeeded but llama-server binary not found in expected locations")
    return None


def ensure_llama_server() -> str | None:
    """
    Return the path to a usable ``llama-server`` executable, or None if
    every detection / install strategy fails.
    """
    # 0. Explicit path override
    if LLAMA_SERVER_PATH:
        p = Path(LLAMA_SERVER_PATH)
        if p.is_file() and os.access(p, os.X_OK):
            _log_install_fmt("using LLAMA_SERVER_PATH: %s", p)
            return str(p)
        _log_install_fmt("LLAMA_SERVER_PATH set but not executable: %s", p)

    # 1. Existing on PATH or common locations
    found = _find_existing_llama_server()
    if found:
        return found

    # 2. Cached install
    found = _find_cached_llama_server()
    if found:
        return found

    if not AUTO_BUILD:
        _log_install("AUTO_BUILD_LLAMA_SERVER is disabled, not installing")
        return None

    # 3. Prebuilt binary from GitHub
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    found = _download_prebuilt_llama_server()
    if found:
        return found

    # 4. Build from source
    found = _build_llama_server_from_source()
    if found:
        return found

    _log_install("ALL INSTALL STRATEGIES FAILED — llama-server is not available")
    return None


# ---------------------------------------------------------------------------
# 8.  Server lifecycle
# ---------------------------------------------------------------------------

def start_llama_server(name: str, cfg: dict[str, Any], llama_server_bin: str) -> bool:
    """Launch a llama-server subprocess.  Returns True on success."""
    port = cfg["port"]
    ctx = cfg["ctx"]

    if is_port_open(port):
        _log.info("%s: port %d is already active — assuming already running", name, port)
        _server_ready[name] = True
        return True

    model_path = ensure_model(name, cfg)
    _downloaded_models[name] = model_path

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    out_log = str(LOGS_DIR / f"{name.lower()}.out.log")
    err_log = str(LOGS_DIR / f"{name.lower()}.err.log")

    cmd = [
        llama_server_bin,
        "--host", "127.0.0.1",
        "--port", str(port),
        "-m", model_path,
        "-c", str(ctx),
        "-t", str(LLAMA_THREADS),
        "-b", str(LLAMA_BATCH_SIZE),
    ]

    _log.info("%s: starting: %s", name, " ".join(cmd))
    _log.info("%s: stdout -> %s", name, out_log)
    _log.info("%s: stderr -> %s", name, err_log)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=open(out_log, "w", encoding="utf-8"),
            stderr=open(err_log, "w", encoding="utf-8"),
        )
    except Exception as exc:
        msg = f"Failed to launch {name}: {exc}"
        _log.error(msg)
        _server_errors[name] = msg
        return False

    _server_processes[name] = proc
    return True


def wait_for_server(name: str, port: int, timeout_seconds: int = 120) -> bool:
    """Poll the server until it responds or the timeout expires."""
    _log.info("%s: waiting for server on port %d (timeout=%ds)", name, port, timeout_seconds)

    deadline = time.monotonic() + timeout_seconds
    health_url = f"http://127.0.0.1:{port}/health"
    models_url = f"http://127.0.0.1:{port}/v1/models"

    while time.monotonic() < deadline:
        try:
            resp = requests.get(health_url, timeout=3)
            if resp.status_code < 500:
                _log.info("%s: ready (health OK)", name)
                _server_ready[name] = True
                return True
        except requests.RequestException:
            pass

        try:
            resp = requests.get(models_url, timeout=3)
            if resp.status_code < 500:
                _log.info("%s: ready (v1/models OK)", name)
                _server_ready[name] = True
                return True
        except requests.RequestException:
            pass

        if is_port_open(port):
            _log.info("%s: ready (TCP connect OK)", name)
            _server_ready[name] = True
            return True

        time.sleep(2)

    # Timeout
    err_log = LOGS_DIR / f"{name.lower()}.err.log"
    tail = _tail_log(err_log)
    msg = f"{name} did not become ready within {timeout_seconds}s"
    if tail:
        msg += f"\nLast stderr lines:\n{tail}"
    _log.error("%s: %s", name, msg)
    _server_errors[name] = msg
    return False


def start_all_servers() -> str | None:
    """
    Detect/install llama-server, download models, launch both servers, and
    wait for readiness.  Returns the llama-server binary path, or None if
    the binary itself could not be obtained.
    """
    _log.info("=" * 60)
    _log.info("Starting AshatOS Dual llama-server Host")
    _log.info("=" * 60)

    llama_bin = ensure_llama_server()
    if not llama_bin:
        msg = "llama-server binary could not be found or installed"
        _log.error(msg)
        _server_errors["MainBrain"] = msg
        _server_errors["MicroBrain"] = msg
        return None

    _log.info("llama-server binary: %s", llama_bin)

    for name, cfg in MODELS.items():
        start_llama_server(name, cfg, llama_bin)

    for name, cfg in MODELS.items():
        if _server_processes.get(name):
            wait_for_server(name, cfg["port"])

    ready = [n for n, v in _server_ready.items() if v]
    failed = [n for n, v in _server_ready.items() if not v]
    _log.info("Ready: %s  |  Failed: %s", ready, failed)

    return llama_bin


def stop_all_servers() -> None:
    """Terminate any running llama-server subprocesses."""
    for name, proc in _server_processes.items():
        if proc and proc.poll() is None:
            _log.info("terminating %s (pid %d)", name, proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            _server_processes[name] = None
            _server_ready[name] = False


# ---------------------------------------------------------------------------
# 9.  Inference via HTTP
# ---------------------------------------------------------------------------

def call_llama_server(
    model_name: str,
    prompt: str,
    max_tokens: int = 512,
    temperature: float = 0.7,
    top_p: float = 0.9,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """Send a chat-completion request to the local llama-server."""
    if model_name not in MODELS:
        return {"ok": False, "error": f"Unknown model: {model_name}"}

    if not _server_ready.get(model_name, False):
        return {
            "ok": False,
            "error": f"{model_name} is not ready. Check server status.",
        }

    cfg = MODELS[model_name]
    port = cfg["port"]

    messages: list[dict[str, str]] = []
    sp = (system_prompt or cfg["system_prompt"]).strip()
    if sp:
        messages.append({"role": "system", "content": sp})
    messages.append({"role": "user", "content": prompt.strip()})

    payload = {
        "model": "local",
        "messages": messages,
        "temperature": float(temperature),
        "max_tokens": max(1, min(int(max_tokens), 2048)),
        "top_p": float(top_p),
        "stream": False,
    }

    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    try:
        started = time.perf_counter()
        resp = requests.post(url, json=payload, timeout=120)
        elapsed = time.perf_counter() - started
    except requests.RequestException as exc:
        return {"ok": False, "error": f"HTTP request failed: {exc}"}

    if resp.status_code == 200:
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            return {
                "ok": True,
                "model": model_name,
                "response": text,
                "elapsed_seconds": round(elapsed, 3),
                "usage": data.get("usage", {}),
            }
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"Response parse error: {exc}"}
    else:
        return _call_legacy_completion(port, prompt, max_tokens, temperature, top_p)


def _call_legacy_completion(
    port: int,
    prompt: str,
    max_tokens: int = 512,
    temperature: float = 0.7,
    top_p: float = 0.9,
) -> dict[str, Any]:
    """Fallback to the /completion (non-chat) endpoint."""
    url = f"http://127.0.0.1:{port}/completion"
    payload = {
        "prompt": prompt,
        "n_predict": max(1, min(int(max_tokens), 2048)),
        "temperature": float(temperature),
        "top_p": float(top_p),
    }

    try:
        started = time.perf_counter()
        resp = requests.post(url, json=payload, timeout=120)
        elapsed = time.perf_counter() - started
    except requests.RequestException as exc:
        return {"ok": False, "error": f"Legacy endpoint also failed: {exc}"}

    if resp.status_code == 200:
        try:
            data = resp.json()
            text = data.get("content", data.get("response", ""))
            return {
                "ok": True,
                "model": "legacy",
                "response": text,
                "elapsed_seconds": round(elapsed, 3),
            }
        except (KeyError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"Legacy response parse error: {exc}"}

    return {
        "ok": False,
        "error": f"HTTP {resp.status_code}: {resp.text[:500]}",
    }


# ---------------------------------------------------------------------------
# 10.  Status helpers
# ---------------------------------------------------------------------------

def get_status() -> dict[str, Any]:
    """Return a snapshot of the current server / app state."""
    llama_bin_path: str | None = None
    if LLAMA_SERVER_PATH:
        llama_bin_path = LLAMA_SERVER_PATH
    else:
        which = shutil.which("llama-server")
        if which:
            llama_bin_path = which
        else:
            cached = CACHE_BIN_DIR / "llama-server"
            if cached.is_file():
                llama_bin_path = str(cached)

    lanes: dict[str, dict[str, Any]] = {}
    for name, cfg in MODELS.items():
        proc = _server_processes.get(name)
        running = proc is not None and proc.poll() is None
        error_msg = _server_errors.get(name, "")
        # Include error log tail when server failed
        log_tail = ""
        if error_msg and not _server_ready.get(name, False):
            log_tail = _tail_log(LOGS_DIR / f"{name.lower()}.err.log")
        lanes[name] = {
            "ready": _server_ready.get(name, False),
            "running": running,
            "port": cfg["port"],
            "ctx": cfg["ctx"],
            "model_path": _downloaded_models.get(name, cfg["local_path"] or ""),
            "error": error_msg,
            "error_log_tail": log_tail,
        }

    return {
        "uptime_seconds": round(time.time() - _started_at, 1),
        "llama_server_path": llama_bin_path or "(not found)",
        "lanes": lanes,
        "all_ready": all(l["ready"] for l in lanes.values()),
    }


def status_text() -> str:
    """Render server status as a Markdown string for the UI."""
    st = get_status()
    lines = [
        f"**Uptime:** {st['uptime_seconds']:.0f}s",
        f"**llama-server:** `{st['llama_server_path']}`",
        "",
    ]
    for name, info in st["lanes"].items():
        emoji = "✅" if info["ready"] else ("❌" if info["error"] else "⏳")
        lines.append(f"{emoji} **{name}** (port {info['port']}, ctx {info['ctx']})")
        lines.append(f"   - running: {info['running']}")
        lines.append(f"   - ready: {info['ready']}")
        if info["model_path"]:
            lines.append(f"   - model: `{info['model_path']}`")
        if info["error"]:
            lines.append(f"   - error: `{info['error'][:150]}`")
        if info["error_log_tail"]:
            lines.append("   - last stderr lines:")
            for line in info["error_log_tail"].splitlines():
                lines.append(f"     `{line[:120]}`")
        lines.append("")

    if not st["all_ready"]:
        lines.append("⚠️ **Degraded mode** — some servers are not ready.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 11.  Gradio event handlers
# ---------------------------------------------------------------------------

def handle_chat(model_name: str, message: str, chat_history: list[dict],
                max_tokens: int, temperature: float, top_p: float) -> tuple[list[dict], list[dict], str]:
    """Process a chat message, append to history, return (state, chatbot, "").
    Uses Gradio 6.x "messages" format: [{"role": ..., "content": ...}, ...]"""
    if not message or not message.strip():
        return chat_history, chat_history, ""

    # Append user message to history
    chat_history.append({"role": "user", "content": message})

    result = call_llama_server(
        model_name=model_name,
        prompt=message,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
    )

    if result.get("ok"):
        response = result["response"]
    else:
        response = f"**Error:** {result.get('error', 'unknown error')}"

    chat_history.append({"role": "assistant", "content": response})
    return chat_history, chat_history, ""


def refresh_status() -> str:
    """Return fresh status text."""
    return status_text()


# ---------------------------------------------------------------------------
# 12.  Graceful shutdown
# ---------------------------------------------------------------------------

atexit.register(stop_all_servers)

# ---------------------------------------------------------------------------
# 13.  Gradio UI
# ---------------------------------------------------------------------------

@spaces_gpu
def _zero_gpu_init() -> None:
    """Signal to the zeroGPU runtime that this Space uses GPU resources.
    The decorator keeps the GPU allocated while this function runs.
    Since our llama-server subprocesses need persistent GPU access, this
    function runs the entire server lifecycle and blocks indefinitely."""
    _llama_bin_local = start_all_servers()
    if not _llama_bin_local:
        _log.warning("llama-server not available — UI will launch in degraded mode")
        return
    # Block forever to keep the GPU allocated for the subprocesses.
    import threading
    threading.Event().wait()


# Start servers at import time (before Gradio launches)
# For zeroGPU, this runs inside @spaces_gpu to keep GPU allocated.
import threading
_gpu_thread = threading.Thread(target=_zero_gpu_init, daemon=True)
_gpu_thread.start()
# Give it a moment to start before the UI builds
import time
time.sleep(1)

with gr.Blocks(
    title="AshatOS Dual llama-server",
) as demo:

    gr.Markdown(
        """
        # 🧠 AshatOS Dual llama-server

        Two local GGUF models served by `llama-server` subprocesses.
        """
    )

    with gr.Tab("Chat"):
        chatbot = gr.Chatbot(
            label="Conversation",
            placeholder="Select a model and start chatting...",
            height=400,
        )

        with gr.Row():
            model_selector = gr.Dropdown(
                choices=list(MODELS.keys()),
                value="MainBrain",
                label="Model",
                scale=1,
                interactive=True,
            )
            with gr.Column(scale=4):
                prompt_box = gr.Textbox(
                    label="Your message",
                    placeholder="Type your message here and press Enter or click Send...",
                    lines=2,
                )
            submit_btn = gr.Button("Send", variant="primary", scale=1, min_width=80)

        with gr.Accordion("Parameters", open=False):
            with gr.Row():
                max_tokens_slider = gr.Slider(
                    1, 2048, value=512, step=1, label="Max tokens"
                )
                temperature_slider = gr.Slider(
                    0.0, 2.0, value=0.7, step=0.05, label="Temperature"
                )
                top_p_slider = gr.Slider(
                    0.0, 1.0, value=0.9, step=0.05, label="Top-p"
                )

        clear_btn = gr.Button("🗑 Clear conversation", variant="secondary", size="sm")

        # State to hold conversation history
        chat_state = gr.State([])

        submit_btn.click(
            fn=handle_chat,
            inputs=[
                model_selector, prompt_box, chat_state,
                max_tokens_slider, temperature_slider, top_p_slider,
            ],
            outputs=[chat_state, chatbot, prompt_box],
            api_name="chat",
            concurrency_limit=1,
        )

        # Also trigger on Enter key in the textbox
        prompt_box.submit(
            fn=handle_chat,
            inputs=[
                model_selector, prompt_box, chat_state,
                max_tokens_slider, temperature_slider, top_p_slider,
            ],
            outputs=[chat_state, chatbot, prompt_box],
            concurrency_limit=1,
        )

        clear_btn.click(
            fn=lambda: ([], []),
            inputs=[],
            outputs=[chat_state, chatbot],
        )

    with gr.Tab("Server Status"):
        refresh_btn = gr.Button(
            "🔄 Refresh Status", variant="secondary",
            elem_id="refresh-status-btn",
        )
        status_display = gr.Markdown(value=status_text())
        refresh_btn.click(
            fn=refresh_status,
            inputs=[],
            outputs=status_display,
            api_name="status",
            concurrency_limit=1,
        )

    with gr.Tab("About"):
        gr.Markdown(f"""
        ### AshatOS Dual llama-server Host

        - **MainBrain** port `{MODELS['MainBrain']['port']}` (ctx {MODELS['MainBrain']['ctx']})
        - **MicroBrain** port `{MODELS['MicroBrain']['port']}` (ctx {MODELS['MicroBrain']['ctx']})

        **Model files:**
        - MainBrain: `{MODELS['MainBrain']['file']}` from `{MODELS['MainBrain']['repo']}`
        - MicroBrain: `{MODELS['MicroBrain']['file']}` from `{MODELS['MicroBrain']['repo']}`

        **Logs:** `./logs/`
        - `mainbrain.out.log` / `mainbrain.err.log`
        - `microbrain.out.log` / `microbrain.err.log`
        - `llama_install.log`

        **Environment variables:**
        - `MAINBRAIN_PORT`, `MICROBRAIN_PORT`
        - `MAINBRAIN_CTX`, `MICROBRAIN_CTX`
        - `MAINBRAIN_MODEL_PATH`, `MICROBRAIN_MODEL_PATH`
        - `LLAMA_THREADS`, `LLAMA_BATCH_SIZE`
        - `LLAMA_SERVER_PATH`, `AUTO_BUILD_LLAMA_SERVER`
        """)

demo.queue(default_concurrency_limit=1, max_size=12)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860, theme=gr.themes.Soft())
