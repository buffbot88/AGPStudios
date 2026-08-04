"""Sanitized public status and metrics projection."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from domain import Lane, LaneConfig, lane_cfg
from metrics_store import MetricRecord, MetricsStore


@dataclass
class LaneActivity:
    state: str = "online"
    active_request_started_at: float | None = None
    last_success_at: str | None = None
    last_error_code: str | None = None


LANE_ACTIVITY: dict[str, LaneActivity] = {"brainstem": LaneActivity()}


def _derive_lane_state(
    lane: str,
    model_available: bool,
    summary: dict[str, Any],
    llama_available: bool,
) -> str:
    """Derive a public lane state from local runtime signals."""
    if not llama_available:
        return "offline"
    if not model_available:
        return "waking"
    if summary.get("total_requests", 0) == 0:
        return "online"
    return "online" if summary.get("last_success", True) else "degraded"


@dataclass
class RuntimeState:
    started_at: float
    llama_server_available: bool
    llama_server_path: str | None
    queue_depth: int = 0
    queue_limit: int = 16

    @property
    def uptime_seconds(self) -> float:
        return round(time.time() - self.started_at, 1)


PUBLIC_ERROR_MESSAGES: dict[str, str] = {
    "LOCAL_MODEL_UNAVAILABLE": (
        "The local BrainStem model is unavailable. Check the project logs."
    ),
    "BINARY_INSTALL_FAILED": (
        "The local llama-server binary is unavailable. Check the project logs."
    ),
    "INFERENCE_UNAVAILABLE": (
        "The local inference engine is unavailable. Check the project logs."
    ),
    "BACKEND_START_FAILED": (
        "The local backend failed to start. Check the project logs."
    ),
    "SERVER_START_FAILED": (
        "The local llama-server did not become healthy in time."
    ),
    "INFERENCE_TIMEOUT": "Inference timed out. Retry with a shorter request.",
    "INFERENCE_FAILED": "The local backend returned an inference failure.",
    "INVALID_MODEL_RESPONSE": "The local backend returned an invalid response.",
    "INVALID_REQUEST": "Request validation failed.",
    "UNAUTHORIZED": "Authentication failed.",
    "INTERNAL_ERROR": "Internal server error.",
}


DIAGNOSTIC_PILL_OVERRIDES: dict[str, tuple[str, str]] = {
    "BINARY_INSTALL_FAILED": ("#FB7185", "BINARY MISSING"),
    "LOCAL_MODEL_UNAVAILABLE": ("#FBBF24", "MODEL UNAVAILABLE"),
}

_REDACTED = "<redacted>"


def _redact_path(path: str | None) -> str:
    return os.path.basename(path) if path else "(not found)"


def _redact_string(value: str | None, *, max_len: int = 200) -> str | None:
    if value is None:
        return None
    value = value[:max_len] + ("…" if len(value) > max_len else "")
    lowered = value.lower()
    for needle in (
        "x-ashat-key", "brainstem_key", "hf_token", "hf-token",
        "authorization:", "bearer ",
    ):
        if needle in lowered:
            return _REDACTED
    return value


@dataclass
class PublicSnapshot:
    metrics: MetricsStore
    runtime: RuntimeState
    lane_configs: dict[Lane, LaneConfig] = field(default_factory=dict)

    @classmethod
    def from_metrics(
        cls,
        metrics: MetricsStore,
        runtime: RuntimeState,
        lane_configs: dict[Lane, LaneConfig],
    ) -> "PublicSnapshot":
        return cls(metrics, runtime, lane_configs)

    def render_status(self) -> dict[str, Any]:
        lanes: dict[str, Any] = {}
        for lane in Lane:
            cfg = self.lane_configs.get(lane, lane_cfg(lane))
            available = bool(cfg.model_path and os.path.isfile(cfg.model_path))
            summary = self.metrics.get_summary(lane.value)
            raw_state = _derive_lane_state(
                lane.value, available, summary,
                self.runtime.llama_server_available,
            )
            failure = summary.get("last_failure_code")
            state = "waking" if failure == "LOCAL_MODEL_UNAVAILABLE" else raw_state
            lanes[lane.value] = {
                "label": cfg.label,
                "model": cfg.file,
                "ctx": cfg.ctx,
                "available": available,
                "ready": available,
                "lane_state": state,
                "lane_state_raw": raw_state,
                "last_failure_code": failure,
                "reason_message": PUBLIC_ERROR_MESSAGES.get(failure) if failure else None,
                **summary,
            }
        return {
            "uptime_seconds": self.runtime.uptime_seconds,
            "llama_server_available": self.runtime.llama_server_available,
            "degraded": not self.runtime.llama_server_available,
            "llama_server": _redact_path(self.runtime.llama_server_path),
            "queue": {"depth": self.runtime.queue_depth, "limit": self.runtime.queue_limit},
            "lanes": lanes,
            "all_ready": bool(
                lanes.get("brainstem", {}).get("ready", False)
                and self.runtime.llama_server_available
            ),
        }

    def render_metrics(self) -> dict[str, Any]:
        events = self.metrics.get_events()
        return {
            "uptime_seconds": self.runtime.uptime_seconds,
            "summaries": {"brainstem": self.metrics.get_summary("brainstem")},
            "total_events": len(events),
            "recent_events": [_redact_string(e) for e in events[-20:]],
        }

    def render_html(self) -> str:
        status = self.render_status()
        lines = [
            '<div style="font-family: monospace; padding: 8px;">',
            f"<b>Uptime:</b> {status['uptime_seconds']:.0f}s &nbsp;|&nbsp; "
            f"<b>llama-server:</b> "
            f"{'🟢 available' if status['llama_server_available'] else '🔴 DEGRADED'} "
            f"<code>{status['llama_server']}</code>",
        ]
        info = status["lanes"]["brainstem"]
        lines.append(
            f'<div style="margin: 8px 0; padding: 8px; border: 1px solid #444;">'
            f'<b>{"🟢" if info["ready"] else "🔴"} {info["label"]}</b><br>'
            f'Model: {info["model"]} · Context: {info["ctx"]}<br>'
            f'Requests: {info["total_requests"]} · Success: {info["success_rate"]}%'
            f'</div></div>'
        )
        return "\n".join(lines)

    def render_frames(self) -> dict[str, list[dict[str, Any]]]:
        all_metrics = self.metrics.get_all_metrics()
        return {
            "brainstem": self._to_frame(all_metrics.get("brainstem", [])),
            "events": [{"Event": e} for e in self.metrics.get_events()[-10:]],
        }

    @staticmethod
    def _to_frame(records: list[MetricRecord]) -> list[dict[str, Any]]:
        return [
            {
                "timestamp": record.timestamp,
                "generation_tokens_per_second": record.generation_tokens_per_second,
                "prompt_tokens_per_second": record.prompt_tokens_per_second,
                "total_latency_ms": record.total_latency_ms,
                "time_to_first_token_ms": record.time_to_first_token_ms,
                "success": record.success,
            }
            for record in records[-50:]
        ]
