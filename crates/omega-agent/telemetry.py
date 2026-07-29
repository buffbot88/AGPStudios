"""Telemetry relay — bridges inference pipeline events to the dashboard hero card.

This module is the single authority for transforming raw MetricsStore data into
dashboard-ready telemetry packages. It ensures the hero card always has fresh,
structured data to display — even before the first inference request.

Responsibilities:
    * Seed boot-time telemetry when the server starts.
    * Package the latest MetricRecord(s) into a concise telemetry snapshot.
    * Derive dashboard-friendly fields (state labels, formatted speeds, etc.)
      so the HTML builder never has to interpret raw metrics.
    * Track the "last known good" state so stale cards still show something.

Design:
    * Thread-safe (lock-guarded read of the shared MetricsStore).
    * Zero runtime dependencies beyond the project's own modules.
    * Pure functions where possible; the single :class:`TelemetryRelay` is
      a lightweight facade that owns the last-known-good cache.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from domain import Lane, LaneConfig, lane_cfg
from metrics_store import METRICS, MetricRecord, MetricsStore
from public_snapshot import PublicSnapshot, RuntimeState

_log = logging.getLogger("ashatos")


# ──────────────────────────────────────────────────────────────────────────
# TelemetryPackage — the structured data the hero card consumes
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class TelemetryPackage:
    """A concise, pre-formatted telemetry snapshot for the hero card.

    Every field is populated even when no inference has occurred — the card
    builder can render directly from this without extra null-checking.
    """

    # Host-level
    uptime_seconds: float = 0.0
    host_state: str = "starting"       # operational | starting | degraded | offline
    llama_server: str = "not found"

    # Lane-level
    lane_label: str = "BrainStem"
    lane_state: str = "waking"         # online | busy | waking | degraded | offline
    model_name: str = ""
    short_model: str = ""
    context_size: int = 0

    # Inference metrics (0 / empty before first inference)
    total_requests: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    success_rate: float = 100.0

    generation_tokens_per_second: float = 0.0
    prompt_tokens_per_second: float = 0.0
    fastest_gen_tps: float = 0.0
    slowest_gen_tps: float = 0.0
    avg_latency_ms: float = 0.0

    # Server-side timing (from llama-server timings pipeline)
    time_to_first_token_ms: float | None = None
    avg_time_to_first_token_ms: float | None = None

    # Sparkline data (list of recent gen_tps values)
    recent_gen_speeds: list[float] = field(default_factory=list)
    recent_latencies: list[float] = field(default_factory=list)

    # Timestamps
    last_request_time: str | None = None
    last_success: bool = True

    # Backend info
    backend: str = "cpu"
    gpu_offload_verified: bool = False


# ──────────────────────────────────────────────────────────────────────────
# TelemetryRelay — thread-safe bridge
# ──────────────────────────────────────────────────────────────────────────

class TelemetryRelay:
    """Bridges raw MetricsStore data into hero-card-ready TelemetryPackages.

    Usage::

        relay = TelemetryRelay()
        relay.seed_boot()                    # call once at startup
        pkg = relay.package()                # call on every dashboard tick
        card_html = render_card(pkg)         # your HTML builder

    Thread-safe: all reads from the MetricsStore are lock-guarded.
    """

    def __init__(
        self,
        store: MetricsStore | None = None,
        lane_configs: dict[Lane, LaneConfig] | None = None,
    ) -> None:
        self._store = store or METRICS
        self._lane_configs = lane_configs
        # Last-known-good cache — never null once seed_boot() is called.
        self._last: TelemetryPackage | None = None

    def seed_boot(
        self,
        lane: Lane = Lane.BRAINSTEM,
        *,
        backend: str = "cpu",
        gpu_offload: bool = False,
        lane_state: str = "online",
        host_state: str = "operational",
    ) -> TelemetryPackage:
        """Seed boot-time telemetry so the dashboard shows data immediately.

        Call once during startup after the model path is verified.
        Writes a ``MetricRecord`` into the store and caches the resulting
        package as the "last known good" state.

        ``lane_state`` and ``host_state`` are honest by default ("online" /
        "operational") but may be overridden ("waking" / "degraded" /
        "offline") when boot succeeded mechanically but the lane is not
        actually usable — e.g. HF download failed so the model file is
        absent. The cached :class:`TelemetryPackage` is what the dashboard
        renders during the cold-start window.

        IMPORTANT: when ``lane_state != "online"`` this method only
        updates the cached package + event log — it does NOT write a
        MetricRecord. The startup pipeline is expected to make a single
        dedicated :meth:`RunMetrics.record_failure` call for the broken
        path; double-writing here would inflate downstream failure counts.
        """
        cfg = lane_cfg(lane)
        if lane_state == "online":
            rec = MetricRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                lane=lane.value,
                success=True,
                cold_start=True,
                server_start_ms=0.0,
                model_load_ms=0.0,
                prompt_tokens=0,
                completion_tokens=0,
                prompt_tokens_per_second=0.0,
                generation_tokens_per_second=0.0,
                time_to_first_token_ms=None,
                total_latency_ms=0.0,
                backend=backend,
                gpu_offload_verified=gpu_offload,
                finish_reason="n/a",
            )
            self._store.record(rec)
        else:
            # Boot is *not* at the "online" steady state — e.g. HF credits
            # exhausted or model file missing. Defer the typed failure
            # record to the orchestrator's ``record_failure`` call so we
            # don't double-count failures in the summary.
            _log.info(
                "telemetry: seed_boot for %s lane_state=%s \u2014 "
                "skipping MetricRecord; orchestrator records the typed failure",
                lane.value, lane_state,
            )
        self._store.add_event(
            f"{lane.value}: server ready (backend={backend}, "
            f"gpu_offload={gpu_offload}, state={lane_state})"
        )

        pkg = TelemetryPackage(
            lane_label=cfg.label,
            model_name=cfg.file,
            context_size=cfg.ctx,
            lane_state=lane_state,
            host_state=host_state,
            backend=backend,
            gpu_offload_verified=gpu_offload,
        )
        self._last = pkg
        _log.info(
            "telemetry: boot seed for %s \u2014 model=%s backend=%s lane_state=%s",
            lane.value, cfg.file, backend, lane_state,
        )
        return pkg

    def package(
        self,
        lane: Lane = Lane.BRAINSTEM,
        runtime_state: RuntimeState | None = None,
    ) -> TelemetryPackage:
        """Build a fresh TelemetryPackage from current metric store state.

        Delegates to :class:`PublicSnapshot` for status/metrics derivation,
        then extracts the dashboard-specific ``TelemetryPackage`` fields
        from the snapshot. Eliminates the duplicate summary/metrics reading
        that previously existed between this method and ``PublicSnapshot``.

        If ``runtime_state`` is omitted, a default is built from ``seed_boot``
        metadata (uptime starts at 0, llama-server assumed available).
        """
        if runtime_state is None:
            runtime_state = RuntimeState(
                started_at=time.time() - 1.0,
                llama_server_available=True,
                llama_server_path=None,
            )

        snapshot = PublicSnapshot.from_metrics(
            self._store,
            runtime_state,
            self._lane_configs or {},
        )
        status = snapshot.render_status()
        frames = snapshot.render_frames()

        cfg = lane_cfg(lane)
        lane_key = lane.value
        lane_info = status.get("lanes", {}).get(lane_key, {})

        # Sparkline data from frames (delegated to snapshot)
        lane_frames = frames.get(lane_key, [])
        recent_gen = [
            f["generation_tokens_per_second"]
            for f in lane_frames[-30:]
            if f.get("generation_tokens_per_second", 0) > 0
        ]
        recent_lat = [
            f["total_latency_ms"]
            for f in lane_frames[-30:]
            if f.get("total_latency_ms", 0) > 0
        ]

        lane_state = lane_info.get("lane_state", "waking")

        pkg = TelemetryPackage(
            uptime_seconds=status.get("uptime_seconds", 0.0),
            host_state="operational" if lane_state == "online" else (
                "starting" if lane_state == "waking" else
                "degraded" if lane_state == "degraded" else "offline"
            ),
            llama_server=(
                "available"
                if status.get("llama_server_available")
                else "not found"
            ),
            lane_label=cfg.label,
            lane_state=lane_state,
            model_name=cfg.file,
            context_size=cfg.ctx,
            total_requests=lane_info.get("total_requests", 0),
            total_prompt_tokens=lane_info.get("total_prompt_tokens", 0),
            total_completion_tokens=lane_info.get("total_completion_tokens", 0),
            success_rate=lane_info.get("success_rate", 100.0),
            generation_tokens_per_second=lane_info.get(
                "latest_generation_tokens_per_second", 0.0
            ),
            prompt_tokens_per_second=lane_info.get(
                "avg_prompt_tokens_per_second", 0.0
            ),
            fastest_gen_tps=lane_info.get("quickest_generation_tokens_per_second", 0.0),
            slowest_gen_tps=lane_info.get("slowest_generation_tokens_per_second", 0.0),
            avg_latency_ms=lane_info.get("avg_total_latency_ms", 0.0),
            time_to_first_token_ms=lane_info.get("last_time_to_first_token_ms"),
            avg_time_to_first_token_ms=lane_info.get("avg_time_to_first_token_ms"),
            recent_gen_speeds=recent_gen,
            recent_latencies=recent_lat,
            last_request_time=lane_info.get("last_request_time"),
            last_success=lane_info.get("last_success", True),
            backend=lane_info.get("backend", "cpu"),
            gpu_offload_verified=lane_info.get("gpu_offload_verified", False),
        )

        self._last = pkg
        return pkg

    @property
    def last_package(self) -> TelemetryPackage | None:
        """The most recently built package, or None if never built."""
        return self._last

    # ── private ─────────────────────────────────────────────────────────

    @staticmethod
    def _get_start_time() -> float:
        """Approximate process start time from the first event timestamp."""
        # Fallback: use current time minus 1 (will show 0 uptime briefly)
        return time.time() - 1.0


# ──────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────────

TELEMETRY: TelemetryRelay = TelemetryRelay()
