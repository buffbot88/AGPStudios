"""RunMetrics — sanitizes and records one Run outcome.

Wraps the existing :class:`MetricsStore`. Never stores prompts or generated
text. Categorizes failures consistently so the public dashboard can show
error breakdowns without leaking sensitive strings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import metrics_store as metrics_store_module
from backend_launcher import LiveBackend
from completion_client import CompletionResult
from domain import Lane
from run_errors import RunError

_log = logging.getLogger("ashatos")

# Categories surfaced in the public dashboard. Kept stable as a closed enum
# so the dashboard's pill colors don't drift.
CATEGORIES = (
    "OK",
    "BINARY_INSTALL_FAILED",
    "MODEL_DOWNLOAD_FAILED",
    "INFERENCE_UNAVAILABLE",
    "BACKEND_START_FAILED",
    "SERVER_START_FAILED",
    "GPU_UNAVAILABLE",
    "GPU_OFFLOAD_VERIFICATION_FAILED",
    "INFERENCE_TIMEOUT",
    "INFERENCE_FAILED",
    "INVALID_MODEL_RESPONSE",
    "INVALID_REQUEST",
    "UNAUTHORIZED",
    "INTERNAL_ERROR",
    "CLEANUP_ERROR",
)


@dataclass
class RecordedRun:
    success: bool
    lane: Lane
    error_category: str | None
    cold_start: bool
    server_start_ms: float
    model_load_ms: float
    prompt_tokens: int
    completion_tokens: int
    prompt_tokens_per_second: float
    generation_tokens_per_second: float
    total_latency_ms: float


class RunMetrics:
    """Lightweight façade over MetricsStore for the Run pipeline."""

    def __init__(self, store: metrics_store_module.MetricsStore) -> None:
        self._store = store

    def record_success(
        self,
        lane: Lane,
        backend: LiveBackend,
        completion: CompletionResult,
        total_latency_ms: float,
        cold_start: bool,
    ) -> None:
        rec = metrics_store_module.MetricRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            lane=lane.value,
            success=True,
            cold_start=cold_start,
            server_start_ms=backend.server_start_ms,
            model_load_ms=backend.model_load_ms or 0.0,
            prompt_tokens=completion.prompt_tokens or 0,
            completion_tokens=completion.completion_tokens or 0,
            prompt_tokens_per_second=completion.prompt_tokens_per_second or 0.0,
            generation_tokens_per_second=completion.generation_tokens_per_second or 0.0,
            time_to_first_token_ms=completion.time_to_first_token_ms,
            total_latency_ms=total_latency_ms,
            backend=backend.backend_mode,
            gpu_offload_verified=backend.gpu_offload_verified,
            finish_reason=completion.finish_reason or "stop",
        )
        self._store.record(rec)
        self._store.add_event(
            f"{lane.value}: inference completed "
            f"({rec.prompt_tokens}+{rec.completion_tokens} tokens)"
        )

    def record_boot(
        self,
        lane: Lane,
        *,
        backend_mode: str = "cpu",
        gpu_offload_verified: bool = False,
        server_start_ms: float = 0.0,
        model_load_ms: float = 0.0,
        total_latency_ms: float = 0.0,
    ) -> None:
        """Seed an initial metric record at boot time.

        Called during startup after the binary and model are confirmed
        available. Gives the dashboard data to display (model name,
        backend mode, GPU status) before any inference request arrives.
        All token counts default to 0 since no inference has run yet.
        """
        rec = metrics_store_module.MetricRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            lane=lane.value,
            success=True,
            cold_start=True,
            server_start_ms=server_start_ms,
            model_load_ms=model_load_ms,
            prompt_tokens=0,
            completion_tokens=0,
            prompt_tokens_per_second=0.0,
            generation_tokens_per_second=0.0,
            time_to_first_token_ms=None,
            total_latency_ms=total_latency_ms,
            backend=backend_mode,
            gpu_offload_verified=gpu_offload_verified,
            finish_reason="n/a",
        )
        self._store.record(rec)
        self._store.add_event(
            f"{lane.value}: server ready (backend={backend_mode}, "
            f"gpu_offload={gpu_offload_verified})"
        )

    def record_failure(
        self,
        lane: Lane | None,
        request_id: str,
        error: RunError,
        elapsed_ms: float,
        cold_start: bool = False,
    ) -> None:
        rec = metrics_store_module.MetricRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            lane=lane.value if lane else "unknown",
            success=False,
            cold_start=cold_start,
            total_latency_ms=elapsed_ms,
            error_category=error.code,
        )
        self._store.record(rec)
        # The event log is sanitized — never includes the request_id or the
        # raw exception message; only the lane and a static category.
        lane_label = lane.value if lane else "request"
        self._store.add_event(
            f"{lane_label}: {error.code}"
        )
