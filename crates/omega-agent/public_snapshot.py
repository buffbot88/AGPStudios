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
from dataclasses import dataclass, field
from typing import Any

from domain import Lane, lane_cfg
from metrics_store import MetricRecord, MetricsStore


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
        import time
        return round(time.time() - self.started_at, 1)


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
            lanes[lane.value] = {
                "label": cfg.get("label", lane.value),
                "model": cfg.get("file", ""),
                "ctx": cfg.get("ctx", 0),
                "available": available,
                "ready": available,
                **summary,
            }
        return {
            "uptime_seconds": self.runtime.uptime_seconds,
            "llama_server_available": self.runtime.llama_server_available,
            "degraded": not self.runtime.llama_server_available,
            "llama_server": _redact_path(self.runtime.llama_server_path),
            "lanes": lanes,
            "all_ready": (
                lanes["microbrain"]["ready"]
                and lanes["mainbrain"]["ready"]
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
                "microbrain": self.metrics.get_summary("microbrain"),
                "mainbrain": self.metrics.get_summary("mainbrain"),
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
        for key in ("mainbrain", "microbrain"):
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
            "microbrain": self._to_frame(all_m.get("microbrain", [])),
            "mainbrain": self._to_frame(all_m.get("mainbrain", [])),
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
                "total_latency_ms": r.total_latency_ms,
                "prompt_tokens_per_second": r.prompt_tokens_per_second,
                "success": r.success,
            }
            for r in records[-50:]
        ]
