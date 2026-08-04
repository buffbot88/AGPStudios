"""Persistent local llama-server lifecycle for the BrainStem lane."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from config import CONFIG
from domain import Lane, lane_cfg
from llama_stderr_parser import LlamaServerStderrParser
from run_errors import (
    BackendHealthTimeout,
    BackendStartError,
    CleanupError,
    GpuAllocationError,
    GpuOffloadVerificationError,
    LocalModelUnavailableError,
    RunError,
)

_log = logging.getLogger("ashatos")


def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    """True iff ``host:port`` accepts a TCP connect within one second."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


@dataclass
class LiveBackend:
    """Live local llama-server endpoint descriptor."""

    lane: Lane
    process: subprocess.Popen
    base_url: str
    model_path: str
    server_start_ms: float
    model_load_ms: float | None
    backend_mode: str
    gpu_offload_verified: bool = False
    gpu_offload_layers: tuple[int, int] | None = None
    raw_log_lines: list[str] = field(default_factory=list)
    parser: "LlamaServerStderrParser | None" = None

    def __enter__(self) -> "LiveBackend":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """Terminate the subprocess safely."""
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
            except Exception as exc:
                raise CleanupError(f"kill after terminate-timeout failed: {exc}")
        except Exception as exc:
            raise CleanupError(f"subprocess terminate failed: {exc}")


class BackendLauncher:
    """Manage one persistent CPU llama-server process."""

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
        self._backend: LiveBackend | None = None
        self._lock = threading.RLock()

    def ensure_model(self, lane: Lane) -> str:
        """Require and return the configured local GGUF model path."""
        path = CONFIG.model_path.strip()
        if not path or not os.path.isfile(path):
            raise LocalModelUnavailableError(
                f"{lane.value}: configured local model is missing"
            )
        lane_cfg(lane).model_path = path
        return path

    def ensure_started(
        self, lane: Lane, *, gpu_offload_requested: bool = False,
    ) -> LiveBackend:
        """Return a healthy shared backend, starting it when necessary."""
        with self._lock:
            if self._backend is not None and self._backend.process.poll() is None:
                if self._backend_is_healthy():
                    return self._backend
                self._backend.close()
                self._backend = None
            self._backend = self.launch(
                lane, gpu_offload_requested=gpu_offload_requested,
            )
            return self._backend

    def invalidate(self) -> None:
        """Drop the cached backend after a failed request."""
        with self._lock:
            backend, self._backend = self._backend, None
            if backend is not None:
                backend.close()

    def _backend_is_healthy(self) -> bool:
        if self._backend is None:
            return False
        try:
            response = requests.get(
                f"http://127.0.0.1:{self.port}/health", timeout=2,
            )
            return response.status_code == 200
        except requests.RequestException:
            return False

    def stop(self) -> None:
        with self._lock:
            backend, self._backend = self._backend, None
            if backend is not None:
                backend.close()

    def launch(
        self, lane: Lane, *, gpu_offload_requested: bool = True,
    ) -> LiveBackend:
        """Start the local llama-server and wait for its health endpoint."""
        binary = self._binary_path_getter()
        if not binary or not Path(binary).is_file():
            raise GpuAllocationError(f"local llama-server unavailable at {binary!r}")

        model_path = self.ensure_model(lane)
        cmd = self._build_command(
            binary, model_path, lane_cfg(lane).ctx,
            gpu_offload=gpu_offload_requested,
        )
        start_t = time.perf_counter()
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        except Exception as exc:
            raise BackendStartError(f"Popen failed: {type(exc).__name__}: {exc}")

        parser = LlamaServerStderrParser()
        threading.Thread(
            target=_stderr_reader_loop,
            args=(proc.stderr, parser),
            name=f"llama-stderr-reader-{lane.value}",
            daemon=True,
        ).start()
        load_ms = round((time.perf_counter() - start_t) * 1000, 1)

        try:
            if not self._wait_for_health(self.port, timeout=60.0):
                snapshot = parser.finalize()
                _log.warning(
                    "%s: local backend health timeout; mode=%s lines=%s",
                    lane.value, snapshot.parsed_mode, snapshot.raw_lines_kept,
                )
                raise BackendHealthTimeout(
                    f"local llama-server did not become healthy on port {self.port}"
                )
            parser.await_offload(timeout=2.0)
            result = parser.finalize()
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
            raise

        if gpu_offload_requested and not result.offload_succeeded:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
            raise GpuOffloadVerificationError(
                f"local llama-server did not confirm GPU offload: {result.parsed_mode}"
            )

        backend_mode = result.parsed_mode if result.parsed_mode != "unknown" else "cpu"
        server_start_ms = round((time.perf_counter() - start_t) * 1000, 1)
        return LiveBackend(
            lane=lane,
            process=proc,
            base_url=f"http://127.0.0.1:{self.port}/v1",
            model_path=model_path,
            server_start_ms=server_start_ms,
            model_load_ms=load_ms,
            backend_mode=backend_mode,
            gpu_offload_verified=result.offload_succeeded,
            gpu_offload_layers=result.offloaded_layers,
            raw_log_lines=list(parser.raw),
            parser=parser,
        )

    def _build_command(
        self, binary: str, model_path: str, ctx: int,
        gpu_offload: bool = False,
    ) -> list[str]:
        return [
            binary, "--host", "127.0.0.1", "--port", str(self.port),
            "-m", model_path, "-c", str(ctx), "-t", str(self.n_threads),
            "-b", str(self.n_batch), "-ngl", "999" if gpu_offload else "0",
        ]

    def _wait_for_health(
        self, port: int, timeout: float = 60.0, interval: float = 0.25,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = requests.get(
                    f"http://127.0.0.1:{port}/health", timeout=2,
                )
                if response.status_code < 500:
                    return True
            except requests.RequestException:
                pass
            if is_port_open(port):
                return True
            time.sleep(interval)
        return False


def _stderr_reader_loop(stderr_file, parser: LlamaServerStderrParser) -> None:
    """Drain llama-server stderr into the parser."""
    if stderr_file is None:
        return
    try:
        for raw_line in iter(stderr_file.readline, b""):
            parser.feed(raw_line.decode("utf-8", errors="replace"))
    except Exception as exc:
        _log.info("local llama stderr reader stopped: %s: %s", type(exc).__name__, exc)
    finally:
        try:
            stderr_file.close()
        except Exception:
            pass
