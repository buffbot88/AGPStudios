"""BackendLauncher — per-request llama-server lifecycle owner.

Owns subprocess.Popen, port health-polling, backend-mode detection, GPU-layer
verification from stderr (deferred to a post-Run tick — today the backend
mode is simple ``cuda`` if CUDA_VISIBLE_DEVICES is set), and safe terminate.

The :class:`LiveBackend` value object returned by ``launch()`` carries just
what the downstream completion client needs: a ``base_url``, the model path
the server loaded, and the startup cost in milliseconds.

Maintains back-compat: this module owns the same lifecycle semantics as the
old inline code in ``execute_lane_inner``. Errors translate to typed
exceptions from :mod:`run_errors` so callers don't have to know about
``subprocess`` or ``urllib``.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from huggingface_hub import hf_hub_download

import requests as _requests

from domain import Lane, lane_cfg
from llama_stderr_parser import LlamaServerStderrParser
from run_errors import (
    BackendHealthTimeout,
    BackendStartError,
    CleanupError,
    GpuAllocationError,
    GpuOffloadVerificationError,
    ModelDownloadError,
)

_log = logging.getLogger("ashatos")


def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    """True iff ``host:port`` accepts a TCP connect within 1 second."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


@dataclass
class LiveBackend:
    """Live endpoint descriptor returned by :meth:`BackendLauncher.launch`."""

    lane: Lane
    process: subprocess.Popen
    base_url: str
    model_path: str
    server_start_ms: float
    model_load_ms: float | None
    backend_mode: str
    gpu_offload_verified: bool
    gpu_offload_layers: tuple[int, int] | None = None
    raw_log_lines: list[str] = field(default_factory=list)
    parser: "LlamaServerStderrParser | None" = None

    def __enter__(self) -> "LiveBackend":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """Terminate the subprocess safely; tolerate already-dead."""
        proc = self.process
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception as cleanup_exc:
                raise CleanupError(
                    f"kill after terminate-timeout failed: {cleanup_exc}"
                )
        except Exception as cleanup_exc:
            # CleanupError here is non-fatal; callers should not bubble it
            # up — log + swallow. The orchestrator's outermost try/finally
            # pattern guarantees cleanup even on exceptions during inference.
            raise CleanupError(
                f"subprocess terminate failed: {cleanup_exc}"
            )


class BackendLauncher:
    """Per-request lifecycle for a single llama-server process.

    Stateless across requests; instantiated once at module import and
    shared.
    """

    def __init__(
        self,
        binary_path_getter: Callable[[], str | None],
        port: int,
        n_threads: int,
        n_batch: int,
    ) -> None:
        self._binary_path_getter = binary_path_getter
        self.port = port
        self.n_threads = n_threads
        self.n_batch = n_batch

    # ── Public ────────────────────────────────────────────────────────

    def ensure_model(self, lane: Lane) -> str:
        """Resolve (download if necessary) the GGUF path for ``lane``."""
        cfg = lane_cfg(lane)
        # Env override wins.
        env_key = f"{lane.value.upper()}_MODEL_PATH"
        env_path = os.getenv(env_key, "").strip()
        if env_path and os.path.isfile(env_path):
            cfg["model_path"] = env_path
            return env_path
        # Cached path wins.
        if cfg["model_path"] and os.path.isfile(cfg["model_path"]):
            return cfg["model_path"]

        token = os.getenv("HF_TOKEN") or None
        repo = cfg["repo"]
        filename = cfg["file"]

        # Check if this is a storage bucket (path starts with "buckets/")
        if repo.startswith("buckets/"):
            return self._ensure_bucket_model(lane, repo, filename, token)

        # Standard HF Hub model download.
        _log.info("%s: downloading %s/%s ...", lane.value, repo, filename)
        try:
            path = hf_hub_download(
                repo_id=repo,
                filename=filename,
                revision=os.getenv("MODEL_REVISION", "main"),
                token=token,
            )
        except Exception as exc:
            raise ModelDownloadError(
                f"{lane.value}: HF Hub download failed: {type(exc).__name__}: {exc}"
            )
        cfg["model_path"] = path
        _log.info("%s: downloaded to %s", lane.value, path)
        return path

    def _ensure_bucket_model(
        self, lane: Lane, repo: str, filename: str, token: str | None,
    ) -> str:
        """Download a GGUF from a HuggingFace storage bucket.

        Storage buckets have a different URL pattern than model repos:
        ``https://huggingface.co/buckets/{owner}/{bucket}/resolve/{filename}``

        The ``buckets/`` prefix in ``repo`` identifies the bucket path.
        Example repo: ``buckets/stressthismess/ashatos-storage``
        """
        # Strip "buckets/" prefix to get the bucket identifier
        bucket_id = repo[len("buckets/"):]  # e.g. "stressthismess/ashatos-storage"
        bucket_url = (
            f"https://huggingface.co/buckets/{bucket_id}/resolve/{filename}"
        )

        # Cache path: same cache dir used for model repos
        cache_dir = Path.home() / ".cache" / "ashatos" / "models"
        cache_dir.mkdir(parents=True, exist_ok=True)
        dest = cache_dir / filename

        if dest.is_file() and dest.stat().st_size > 0:
            _log.info("%s: bucket model already cached at %s", lane.value, dest)
            cfg = lane_cfg(lane)
            cfg["model_path"] = str(dest)
            return str(dest)

        _log.info(
            "%s: downloading from bucket %s ...", lane.value, bucket_url,
        )
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = _requests.get(
                bucket_url,
                headers=headers,
                timeout=600,
                stream=True,
            )
            resp.raise_for_status()

            # Stream to disk with progress
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total and downloaded % (10 * 1024 * 1024) == 0:
                            _log.info(
                                "%s: downloaded %d/%d MB (%.0f%%)",
                                lane.value,
                                downloaded // (1024 * 1024),
                                total // (1024 * 1024),
                                downloaded / total * 100,
                            )
        except Exception as exc:
            # Clean up partial download on failure
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            raise ModelDownloadError(
                f"{lane.value}: bucket download failed: "
                f"{type(exc).__name__}: {exc}"
            )

        _log.info(
            "%s: downloaded from bucket to %s (%d MB)",
            lane.value, dest, dest.stat().st_size // (1024 * 1024),
        )
        cfg = lane_cfg(lane)
        cfg["model_path"] = str(dest)
        return str(dest)

    def launch(
        self, lane: Lane, *, gpu_offload_requested: bool = True,
    ) -> LiveBackend:
        """Boot a llama-server process for the lane. Caller must ``close()``.

        Pipeline:
            1. Drain a small stderr parser from a background reader thread
               started the moment the subprocess is up.
            2. Wait for ``/health`` to return 2xx.
            3. If GPU offload was requested, parse the captured lines for
               ``llm_load_tensors: offloaded N/M layers to GPU`` and raise
               :class:`GpuOffloadVerificationError` if absent.
            4. Build the :class:`LiveBackend` from the *parsed* mode, not
               from a ``CUDA_VISIBLE_DEVICES`` env-var inference.
        """
        binary = self._binary_path_getter()
        if not binary or not Path(binary).is_file():
            # The orchestrator's degraded-mode gate must catch this, but
            # double-check here as a defense-in-depth.
            raise GpuAllocationError(
                f"llama-server binary unavailable at: {binary!r}"
            )

        cfg = lane_cfg(lane)
        try:
            model_path = self.ensure_model(lane)
        except ModelDownloadError:
            raise

        _offload = gpu_offload_requested
        cmd = self._build_command(binary, model_path, cfg["ctx"],
                                  gpu_offload=_offload)
        start_t = time.perf_counter()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,  # was DEVNULL — now drained by parser
            )
        except Exception as exc:
            raise BackendStartError(
                f"Popen failed: {type(exc).__name__}: {exc}"
            )

        # Start the stderr reader thread. The parser is the single source
        # of truth for backend mode + offload verification.
        parser = LlamaServerStderrParser()
        reader_thread = threading.Thread(
            target=_stderr_reader_loop,
            args=(proc.stderr, parser),
            name=f"llama-stderr-reader-{lane.value}",
            daemon=True,
        )
        reader_thread.start()

        load_ms = round((time.perf_counter() - start_t) * 1000, 1)

        try:
            healthy = self._wait_for_health(self.port, timeout=30.0)
            if not healthy:
                try:
                    proc.terminate(); proc.wait(timeout=5)
                except Exception:
                    pass
                # Surface the parser's findings for diagnostics even on
                # health-timeout failure.
                snap = parser.finalize()
                _log.warning(
                    "%s: backend health timeout. "
                    "stderr parser: mode=%s offloaded=%s lines=%d",
                    lane.value, snap.parsed_mode, snap.offloaded_layers,
                    snap.raw_lines_kept,
                )
                raise BackendHealthTimeout(
                    f"llama-server did not become healthy on port {self.port}"
                )

            # Wait for the offload line deterministically via the
            # parser's threading.Event (replaces old timing-based drain).
            parser.await_offload(timeout=2.0)
            result = parser.finalize()
        except Exception:
            # On any error from this branch, terminate the subprocess so we
            # don't leak it (the reader thread will exit once stderr EOFs).
            try:
                proc.terminate(); proc.wait(timeout=5)
            except Exception:
                pass
            raise

        if gpu_offload_requested and not result.offload_succeeded:
            # Offload was requested but never confirmed in stderr. Mirror
            # the parser findings into the log and raise.
            _log.warning(
                "%s: GPU offload verification FAILED. "
                "stderr parser: mode=%s offloaded=%s lines=%s",
                lane.value,
                result.parsed_mode,
                result.offloaded_layers,
                repr(result.backends_seen),
            )
            try:
                proc.terminate(); proc.wait(timeout=5)
            except Exception:
                pass
            raise GpuOffloadVerificationError(
                f"llama-server started but did not confirm GPU offload "
                f"(mode={result.parsed_mode}, offloaded={result.offloaded_layers}). "
                f"Set CUDA_VISIBLE_DEVICES, n_gpu_layers>0, or call "
                f"launch(gpu_offload_requested=False) to opt out of verification."
            )

        server_start_ms = round((time.perf_counter() - start_t) * 1000, 1)
        gpu_ok = result.offload_succeeded
        backend_mode = result.parsed_mode if result.parsed_mode != "unknown" else "cpu"
        _log.info(
            "%s: backend=%s gpu_offload=%s layers=%s",
            lane.value, backend_mode, gpu_ok, result.offloaded_layers,
        )

        return LiveBackend(
            lane=lane,
            process=proc,
            base_url=f"http://127.0.0.1:{self.port}/v1",
            model_path=model_path,
            server_start_ms=server_start_ms,
            model_load_ms=load_ms,
            backend_mode=backend_mode,
            gpu_offload_verified=gpu_ok,
            gpu_offload_layers=result.offloaded_layers,
            raw_log_lines=list(parser.raw),
            parser=parser,
        )

    # ── Private ───────────────────────────────────────────────────────

    def _build_command(self, binary: str, model_path: str, ctx: int,
                        gpu_offload: bool = True) -> list[str]:
        return [
            binary,
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "-m", model_path,
            "-c", str(ctx),
            "-t", str(self.n_threads),
            "-b", str(self.n_batch),
            "-ngl", "999" if gpu_offload else "0",
        ]

    def _wait_for_health(
        self, port: int, timeout: float = 30.0, interval: float = 0.25,
    ) -> bool:
        # Lazy import — requests is heavy.
        import requests
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


# ──────────────────────────────────────────────────────────────────────────
# Module-level helpers — stderr reader loop and post-health drain.
# ──────────────────────────────────────────────────────────────────────────

def _stderr_reader_loop(stderr_file, parser: LlamaServerStderrParser) -> None:
    """Drain subprocess stderr into ``parser`` line-by-line until EOF.

    Runs on a daemon thread spawned by :meth:`BackendLauncher.launch`. The
    parser is thread-safe-by-convention (``feed`` does not mutate shared
    state outside ``_buffer`` and ``_backends`` but those mutations are
    serialised because we never call ``feed`` from two threads at once).
    """
    if stderr_file is None:
        return
    try:
        for raw_line in iter(stderr_file.readline, b""):
            try:
                text = raw_line.decode("utf-8", errors="replace")
            except Exception:
                continue
            parser.feed(text)
    except Exception as exc:
        _log.info("llama: stderr reader stopped: %s: %s",
                  type(exc).__name__, exc)
    finally:
        try:
            stderr_file.close()
        except Exception:
            pass


