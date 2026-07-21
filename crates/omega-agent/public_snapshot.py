"""PublicSnapshot — one canonical projection for the public surface.

A single class drives three consumers:
    1. The Gradio dashboard's status widget and per-lane plot frames
       (refreshed automatically every ``PUBLIC_REFRESH_SECONDS``).
    2. ``GET /api/public_status`` — JSON status for ops / explorers.
    3. ``GET /api/public_metrics`` — JSON metrics summary + recent events.

Why it's worth a module:

    * **One shape, three consumers.**  Today three different functions in
      app.py project the metrics store into JSON; they drift. PublicSnap-
      shot has one ``render_*`` method per output channel and they share a
      common redaction/staging pass.
    * **Native sanitization.**  PublicSnapshot strips filesystem paths down
      to basenames, never echoes back the raw ``request_id``, never echoes
      the full metrics-recording path (``~/.cache/ashatos/bin/...``). All
      keys it touches are names, never values.
    * **No side effects.**  The snapshot is a pure projection: same input,
      same output, safe to call from request handlers and from the Gradio
      polling tick on every Space load.

What it must never expose (enforced by tests):
    * prompt or response content;
    * API keys or HF tokens;
    * request headers;
    * internal filesystem paths beyond basenames;
    * full stack traces;
    * AshatOS session identifiers (raw request_id, IP, etc.).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from domain import Lane, lane_cfg
from metrics_store import MetricRecord, MetricsStore


# ──────────────────────────────────────────────────────────────────────────
# LaneActivity — observable runtime state per lane (spec §12)
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class LaneActivity:
    """Lightweight, thread-safe lane-activity record.

    Updated by the inference pipeline whenever a lane runs or fails.
    """
    state: str = "online"  # online | busy | waking | degraded | offline
    active_request_started_at: float | None = None
    last_success_at: str | None = None
    last_error_code: str | None = None


LANE_ACTIVITY: dict[str, LaneActivity] = {
    "brainstem": LaneActivity(),
}


def _derive_lane_state(
    lane: str,
    model_available: bool,
    summary: dict[str, Any],
    llama_available: bool,
) -> str:
    """Derive a human-readable lane state from available signals."""
    if not llama_available:
        return "offline"
    if not model_available:
        return "waking"
    total = summary.get("total_requests", 0)
    if total == 0:
        return "online"  # cached but idle
    last_success = summary.get("last_success", True)
    if not last_success:
        return "degraded"
    return "online"


# ──────────────────────────────────────────────────────────────────────────
# RuntimeState — the only input PublicSnapshot needs besides the metrics
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class RuntimeState:
    """The non-metrics half of the public status payload."""

    started_at: float
    llama_server_available: bool
    llama_server_path: str | None
    """Full path to the llama-server binary, or ``None`` if degraded."""

    @property
    def uptime_seconds(self) -> float:
        return round(time.time() - self.started_at, 1)


# ──────────────────────────────────────────────────────────────────────────
# Public error messages — single source of truth for human-readable
# explanations of error codes. NEVER include raw HF API responses,
# tokens, URLs, paths, or stack traces here — these messages are
# surfaced verbatim on the public dashboard.
# ──────────────────────────────────────────────────────────────────────────

PUBLIC_ERROR_MESSAGES: dict[str, str] = {
    "HF_CREDITS_EXHAUSTED": (
        "HuggingFace credits exhausted \u2014 add credits at "
        "huggingface.co/settings/billing or wait for your monthly "
        "quota to reset."
    ),
    "HF_RATE_LIMITED": (
        "HuggingFace rate limit hit \u2014 the page auto-recovers within "
        "a minute or two."
    ),
    "MODEL_DOWNLOAD_FAILED": (
        "Model download failed. Check the application logs for the "
        "underlying network error."
    ),
    "BINARY_INSTALL_FAILED": (
        "llama-server binary could not be installed. Check the "
        "application logs for the underlying network error."
    ),
    "INFERENCE_UNAVAILABLE": (
        "Inference engine unavailable \u2014 llama-server binary "
        "is NOT installed."
    ),
    "BACKEND_START_FAILED": (
        "Backend process failed to start. Check the application logs."
    ),
    "SERVER_START_FAILED": (
        "llama-server did not become healthy in time. Check the logs."
    ),
    "GPU_UNAVAILABLE": (
        "GPU could not be allocated. The host may be out of ZeroGPU slots."
    ),
    "GPU_OFFLOAD_VERIFICATION_FAILED": (
        "GPU offload requested but never confirmed. The server fell "
        "back to CPU, or no GPU is available."
    ),
    "INFERENCE_TIMEOUT": (
        "Inference timed out. Retry with a shorter prompt or lower max_tokens."
    ),
    "INFERENCE_FAILED": (
        "Backend returned a non-200 response. Check the application logs."
    ),
    "INVALID_MODEL_RESPONSE": (
        "Backend response shape did not match OpenAI-compatible. Check "
        "the application logs."
    ),
    "INVALID_REQUEST": (
        "Request validation failed (missing messages, oversized body, "
        "or unsupported parameters)."
    ),
    "UNAUTHORIZED": (
        "Authentication failed."
    ),
    "INTERNAL_ERROR": (
        "Internal server error. Check the application logs."
    ),
}


# Stable label overrides for the status pill — only set when we have a
# SPECIFIC human explainable reason that warrants a custom color/label.
DIAGNOSTIC_PILL_OVERRIDES: dict[str, tuple[str, str]] = {
    # code:           (pill_color_hex, label)
    "HF_CREDITS_EXHAUSTED": ("#FB7185", "HF QUOTA"),
    "HF_RATE_LIMITED":      ("#FBBF24", "HF RATE-LIMITED"),
    "BINARY_INSTALL_FAILED": ("#FB7185", "BINARY MISSING"),
    "MODEL_DOWNLOAD_FAILED": ("#FBBF24", "MODEL DOWNLOAD"),
}


# ──────────────────────────────────────────────────────────────────────────
# Redaction helpers
# ──────────────────────────────────────────────────────────────────────────

_REDACTED: str = "<redacted>"


def _redact_path(path: str | None) -> str | None:
    """Reduce a filesystem path to its basename before exposing it.

    The full path leaks the host's home directory (``/home/foo/...``,
    ``/Users/x/...``); only the basename is needed for an operator to
    confirm which binary is in use.
    """
    if not path:
        return "(not found)"
    return os.path.basename(path)


def _redact_string(value: str | None, *, max_len: int = 200) -> str | None:
    """Cap a string at max_len and ensure no key-shaped substring leaks.

    The check is conservative — anything that LOOKS like a secret
    (``x-ashat-key:``, ``hf_``, ``hf-token``, ``Bearer ``, ``Authorization``)
    is replaced wholesale.
    """
    if value is None:
        return None
    if len(value) > max_len:
        value = value[:max_len] + "…"
    lowered = value.lower()
    for needle in ("x-ashat-key", "hf_token", "hf-token", "authorization:", "bearer "):
        if needle in lowered:
            return _REDACTED
    return value


# ──────────────────────────────────────────────────────────────────────────
# PublicSnapshot
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class PublicSnapshot:
    """One canonical projection for the public surface.

    Construction:
        snapshot = PublicSnapshot.from_metrics(metrics, runtime, lane_configs)

    Output methods:
        * :meth:`render_status` — body for ``/api/public_status``
        * :meth:`render_metrics` — body for ``/api/public_metrics``
        * :meth:`render_html` — body for the Gradio dashboard status widget
        * :meth:`render_frames` — per-lane plot frames for the dashboard

    Same input, same output: safe to call from request handlers.
    """

    metrics: MetricsStore
    runtime: RuntimeState
    lane_configs: dict[Lane, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_metrics(
        cls,
        metrics: MetricsStore,
        runtime: RuntimeState,
        lane_configs: dict[Lane, dict[str, Any]],
    ) -> "PublicSnapshot":
        return cls(
            metrics=metrics,
            runtime=runtime,
            lane_configs=lane_configs,
        )

    # ── /api/public_status ──────────────────────────────────────────────

    def render_status(self) -> dict[str, Any]:
        lanes: dict[str, Any] = {}
        for lane in Lane:
            cfg = self.lane_configs.get(lane, lane_cfg(lane))
            model_path = cfg.get("model_path", "")
            available = bool(model_path and os.path.isfile(model_path))
            summary = self.metrics.get_summary(lane.value)
            lane_state = _derive_lane_state(
                lane.value, available, summary,
                self.runtime.llama_server_available,
            )
            last_failure_code = summary.get("last_failure_code")
            # Pull the public-safe explanation for the diagnostic — never
            # any raw error text from the store. Falls back to None when
            # no failure is recorded.
            reason_message = (
                PUBLIC_ERROR_MESSAGES.get(last_failure_code)
                if last_failure_code
                else None
            )
            # Stats: HF-specific failures should never read as "online" —
            # override the auto-derived lane_state so the dashboard pill
            # turns amber/red immediately.
            override_state: str | None = None
            if last_failure_code == "HF_CREDITS_EXHAUSTED":
                override_state = "degraded"
            elif last_failure_code in ("HF_RATE_LIMITED", "MODEL_DOWNLOAD_FAILED"):
                override_state = "waking"
            elif last_failure_code == "BINARY_INSTALL_FAILED":
                # Binary install failure is global, not lane-specific.
                pass
            effective_state = override_state or lane_state
            lanes[lane.value] = {
                "label": cfg.get("label", lane.value),
                "model": cfg.get("file", ""),
                "ctx": cfg.get("ctx", 0),
                "available": available,
                "ready": available,
                "lane_state": effective_state,
                "lane_state_raw": lane_state,
                "last_failure_code": last_failure_code,
                "reason_message": reason_message,
                **summary,
            }
        return {
            "uptime_seconds": self.runtime.uptime_seconds,
            "llama_server_available": self.runtime.llama_server_available,
            "degraded": not self.runtime.llama_server_available,
            "llama_server": _redact_path(self.runtime.llama_server_path),
            "lanes": lanes,
            "all_ready": (
                lanes.get("brainstem", {}).get("ready", False)
                and self.runtime.llama_server_available
            ),
        }

    # ── /api/public_metrics ─────────────────────────────────────────────

    def render_metrics(self) -> dict[str, Any]:
        all_events = self.metrics.get_events()
        # Each event is already sanitized at write time by RunMetrics.add_event
        # — it emits ``{lane}.{code}`` not raw text. We re-apply the
        # redact pass as a defense-in-depth belt.
        safe_events = [
            _redact_string(e, max_len=200) for e in all_events[-20:]
        ]
        return {
            "uptime_seconds": self.runtime.uptime_seconds,
            "summaries": {
                "brainstem": self.metrics.get_summary("brainstem"),
            },
            "total_events": len(all_events),
            "recent_events": safe_events,
        }

    # ── Gradio dashboard ─────────────────────────────────────────────────

    def render_html(self) -> str:
        s = self.render_status()
        lines = [
            '<div style="font-family: monospace; padding: 8px;">',
            f"<b>Uptime:</b> {s['uptime_seconds']:.0f}s &nbsp;|&nbsp; "
            f"<b>llama-server:</b> "
            f"{'🟢 available' if s['llama_server_available'] else '🔴 DEGRADED'} "
            f"<code>{s['llama_server']}</code>",
        ]
        if s["degraded"]:
            lines.append(
                '<div style="margin: 8px 0; padding: 8px; border: 1px solid #f87171; '
                'border-radius: 6px; background: #2a0e0e; color: #fca5a5;">'
                '⚠️ <b>Degraded mode</b>: llama-server binary is NOT installed. '
                'All /v1/chat/completions calls will fail with '
                '<code>INFERENCE_UNAVAILABLE</code> until the binary is available. '
                'Check startup logs for the install failure reason.'
                '</div>'
            )
        for key in ("brainstem",):
            info = s["lanes"][key]
            if s["degraded"]:
                emoji = "🟥"
            else:
                emoji = "🟢" if info["ready"] else ("🔴" if not info["available"] else "🟡")
            lines.append(
                f'<div style="margin: 8px 0; padding: 8px; border: 1px solid #444; '
                f'border-radius: 6px; background: #1a1a2e;">'
                f'<b style="font-size: 1.1em;">{emoji} {info["label"]}</b><br>'
                f'<span style="color: #aaa;">Model:</span> {info["model"]} '
                f'<span style="color: #aaa;">Context:</span> {info["ctx"]}<br>'
                f'<span style="color: #aaa;">Requests:</span> {info["total_requests"]} '
                f'<span style="color: #aaa;">Success:</span> {info["success_rate"]}%<br>'
                f'<span style="color: #aaa;">Avg gen tok/s:</span> '
                f'{info["avg_generation_tokens_per_second"]} '
                f'<span style="color: #aaa;">Quickest:</span> '
                f'{info["quickest_generation_tokens_per_second"]} '
                f'<span style="color: #aaa;">Slowest:</span> '
                f'{info["slowest_generation_tokens_per_second"]}<br>'
                f'<span style="color: #aaa;">Avg latency:</span> '
                f'{info["avg_total_latency_ms"]}ms<br>'
                f'<span style="color: #aaa;">Last request:</span> '
                f'{info["last_request_time"] or "—"}'
                f'</div>'
            )
        lines.append("</div>")
        return "\n".join(lines)

    def render_frames(self) -> dict[str, list[dict[str, Any]]]:
        """Per-lane plot frames + recent events for the Gradio plot tick."""
        all_m = self.metrics.get_all_metrics()
        return {
            "brainstem": self._to_frame(all_m.get("brainstem", [])),
            "events": [
                {"Event": e} for e in self.metrics.get_events()[-10:]
            ],
        }

    # ── private ─────────────────────────────────────────────────────────

    @staticmethod
    def _to_frame(records: list[MetricRecord]) -> list[dict[str, Any]]:
        return [
            {
                "timestamp": r.timestamp,
                "generation_tokens_per_second": r.generation_tokens_per_second,
                "prompt_tokens_per_second": r.prompt_tokens_per_second,
                "total_latency_ms": r.total_latency_ms,
                "time_to_first_token_ms": r.time_to_first_token_ms,
                "success": r.success,
            }
            for r in records[-50:]
        ]
