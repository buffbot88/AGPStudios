"""Pure parser for llama-server's startup stderr.

The subprocess writes to stderr in a streaming fashion; we collect those lines
into a bounded buffer and parse them out into typed attributes:

    * :attr:`parsed_mode` — ``cpu`` / ``cuda`` / ``rocm`` / ``vulkan`` / ``metal``
      / ``unknown`` — derived from the ``<BACKEND> buffer size`` lines.
    * :attr:`offloaded_layers` — ``(N, M)`` tuple from the
      ``llm_load_tensors: offloaded N/M layers to GPU`` line, or
      ``None`` if not seen yet.
    * :attr:`saw_offload_request_but_no_offload` — ``True`` if the loader
      was asked to use the GPU (``gpu_layers > 0``) but no offloaded line
      appears in the captured window. The launcher uses this to decide
      whether to raise :class:`GpuOffloadVerificationError`.

Why a separate module: the parser is pure (no I/O, no subprocess) so we
can drive it from unit tests with synthetic lines. The launcher feeds it
in a streaming reader thread.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


# Recognised inline log shapes (stable across llama.cpp 2024-2026).
# We compile once at module import.
_RE_OFFLOADED = re.compile(
    r"^llm_load_tensors:\s+offloaded\s+(\d+)\s*/\s*(\d+)\s+layers\s+to\s+GPU\b",
)
_RE_OFFLOADING = re.compile(
    r"^llm_load_tensors:\s+offloading\s+(\d+)\s+repeating\s+layers\s+to\s+GPU\b"
)
_RE_BUFFER = re.compile(
    r"^llm_load_tensors:\s+(?P<backend>[A-Za-z][A-Za-z0-9]*)\s+buffer\s+size\s+="
)
_RE_GPU_LAYERS = re.compile(
    r"n_gpu_layers\s*=\s*(\d+)"
)
# Other backends that may show up alongside CUDA in the loader buffer lines.
_BACKEND_NORMALIZE = {
    "CPU": "cpu",
    "CUDA": "cuda",
    "CUDA0": "cuda",
    "CUDA1": "cuda",
    "ROCm": "rocm",
    "ROCm0": "rocm",
    "Vulkan": "vulkan",
    "Vulkan0": "vulkan",
    "Metal": "metal",
    "Metal0": "metal",
    "SYCL": "sycl",
    "OpenCL": "opencl",
}


@dataclass
class ParseResult:
    """Typed snapshot of a parsed launch."""

    parsed_mode: str = "unknown"
    """``cpu`` / ``cuda`` / ``rocm`` / ``vulkan`` / ``metal`` / ``unknown``."""

    offloaded_layers: Optional[tuple[int, int]] = None
    """``(N, M)`` from the offloaded log line; ``None`` until seen."""

    gpu_layers_requested: int = 0
    """Value parsed from ``n_gpu_layers=...`` if the launcher echoes it."""

    backends_seen: list[str] = field(default_factory=list)
    """All four backend-buffer back-end tags, kept for diagnostics."""

    raw_lines_kept: int = 0
    """How many raw lines made it into the buffer."""

    @property
    def offload_succeeded(self) -> bool:
        """``True`` iff at least one layer was offloaded to GPU."""
        return (
            self.offloaded_layers is not None
            and self.offloaded_layers[0] > 0
        )

    def to_live_backend_fields(self) -> dict:
        """Map to the field names LiveBackend expects."""
        return {
            "backend_mode": self.parsed_mode,
            "gpu_offload_verified": self.offload_succeeded,
            "gpu_offload_layers": (
                self.offloaded_layers
                if self.offloaded_layers is not None
                else (self.gpu_layers_requested, self.gpu_layers_requested)
            ),
        }


class LlamaServerStderrParser:
    """Streaming parser. Call :meth:`feed` for each new stderr line.

    The parser is intentionally tolerant: lines that don't match any known
    shape are silently stored, never raised. Only :meth:`finalize` is strict
    — it returns a :class:`ParseResult` whose fields the launcher
    interprets to decide whether offload actually happened.
    """

    def __init__(self, max_lines: int = 4000) -> None:
        self._max_lines = max_lines
        self._buffer: deque[str] = deque(maxlen=max_lines)
        self._backends: list[str] = []
        self._offloaded: Optional[tuple[int, int]] = None
        self._gpu_layers_requested: int = 0
        self._parsed_mode: str = "unknown"

    # ── public ─────────────────────────────────────────────────────────

    def feed(self, line: str) -> None:
        """Absorb one decoded stderr line."""
        if not line:
            return
        line = line.rstrip("\r\n")
        self._buffer.append(line)
        # Offloaded summary: the keystone signal that offload succeeded.
        # First valid line wins — if more than one ever appears (it shouldn't,
        # but if log corruption produces a second) we keep the first.
        m = _RE_OFFLOADED.match(line)
        if m and self._offloaded is None:
            self._offloaded = (int(m.group(1)), int(m.group(2)))
            return
        # Offloading-in-progress line — informative but doesn't pin success.
        m = _RE_OFFLOADING.match(line)
        if m:
            return
        # Backend buffer size line — pins which backend is active.
        m = _RE_BUFFER.match(line)
        if m:
            backend_raw = m.group("backend")
            self._backends.append(backend_raw)
            norm = _BACKEND_NORMALIZE.get(backend_raw, backend_raw.lower())
            # First non-CPU backend encountered wins; if it's a multi-backend
            # setup we still keep all of them in backends_seen for diagnosis.
            if self._parsed_mode == "unknown":
                self._parsed_mode = norm
            elif self._parsed_mode == "cpu" and norm != "cpu":
                self._parsed_mode = norm
            return
        # Optional: llama.cpp sometimes echoes ``n_gpu_layers=NN`` at start.
        m = _RE_GPU_LAYERS.search(line)
        if m:
            self._gpu_layers_requested = int(m.group(1))

    def feed_many(self, lines: list[str]) -> None:
        for line in lines:
            self.feed(line)

    def finalize(self) -> ParseResult:
        """Return the cumulative :class:`ParseResult`."""
        return ParseResult(
            parsed_mode=self._parsed_mode,
            offloaded_layers=self._offloaded,
            gpu_layers_requested=self._gpu_layers_requested,
            backends_seen=list(self._backends),
            raw_lines_kept=len(self._buffer),
        )

    # ── diagnostics helpers ─────────────────────────────────────────────

    @property
    def raw(self) -> list[str]:
        return list(self._buffer)
