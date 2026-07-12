"""ASHAT Neural Host Homepage — premium public dashboard.

Extracted from app.py per spec §17. Owns:

    * CSS and layout
    * Header with glowing brain badge
    * Two neural-lane cards (MicroBrain pink, MainBrain violet)
    * Inline SVG sparkline rendering
    * gr.Timer refresh lifecycle
    * Responsive breakpoints

Does NOT own:
    * Metrics aggregation (→ MetricsStore)
    * Sanitization / redaction (→ PublicSnapshot)
    * Inference pipeline (→ app.py / run pipeline)
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


from public_snapshot import PublicSnapshot


# ──────────────────────────────────────────────────────────────────────────
# Constants — colour system (spec §13)
# ──────────────────────────────────────────────────────────────────────────

_BG = "#070B14"
_PANEL = "#111827"
_RAISED = "#172033"
_PRIMARY = "#F8FAFC"
_SECONDARY = "#94A3B8"
_MUTED = "#64748B"
_BORDER = "rgba(148,163,184,0.18)"
_GREEN = "#34D399"
_AMBER = "#FBBF24"
_CORAL = "#FB7185"

_MICRO_ACCENT = "#F472B6"
_MICRO_GLOW = "rgba(244,114,182,0.18)"
_MICRO_BRIGHT = "#FB8BC8"

_MAIN_ACCENT = "#8B5CF6"
_MAIN_GLOW = "rgba(139,92,246,0.18)"
_MAIN_BRIGHT = "#A78BFA"


# ──────────────────────────────────────────────────────────────────────────
# Sparkline — inline SVG (spec §7)
# ──────────────────────────────────────────────────────────────────────────

def _build_sparkline(
    values: list[float],
    accent: str,
    lane_state: str,
    *,
    width: int = 280,
    height: int = 52,
) -> str:
    """Render a no-clutter SVG polyline of recent generation speeds."""
    if lane_state in ("offline", "waking", "degraded"):
        labels = {"offline": "Offline", "waking": "Starting...", "degraded": "Degraded"}
        return (
            '<div style="color: %s; font-size: 0.75em; font-family: '
            'sans-serif; padding: 8px 0;">%s</div>'
        ) % (_MUTED, labels.get(lane_state, "Unavailable"))

    # Use last N values, cap at 30
    samples = [v for v in values if v > 0][-30:]
    if not samples:
        return (
            '<div style="color: %s; font-size: 0.75em; font-family: '
            'sans-serif; padding: 8px 0;">Online \u2014 ready</div>'
        ) % _MUTED

    min_v = min(samples)
    max_v = max(samples)
    span = max_v - min_v if max_v > min_v else 1.0

    pad_x = 8
    pad_y = 6
    plot_w = width - 2 * pad_x
    plot_h = height - 2 * pad_y
    n = len(samples)

    def _to_svg(i: int, v: float) -> tuple[float, float]:
        x = pad_x + (i / (n - 1 if n > 1 else 1)) * plot_w
        y = pad_y + plot_h - ((v - min_v) / span) * plot_h
        return x, y

    points = []
    for i, v in enumerate(samples):
        x, y = _to_svg(i, v)
        points.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(points)

    # Latest value label
    last_x, last_y = _to_svg(n - 1, samples[-1])

    svg = (
        '<svg viewBox="0 0 %d %d" style="width: %dpx; height: %dpx; '
        'display: block;" xmlns="http://www.w3.org/2000/svg">'
        '<polyline points="%s" fill="none" stroke="%s" stroke-width="1.5" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="%.1f" cy="%.1f" r="2.5" fill="%s"/>'
        '<text x="%.1f" y="%.1f" fill="%s" font-size="9" font-family="'
        'monospace" font-weight="600" text-anchor="end" '
        'dominant-baseline="auto">%s</text>'
    ) % (
        width, height, width, height,
        polyline, accent,
        last_x, last_y, accent,
        width - pad_x, pad_y + 10, _SECONDARY,
        f"{max_v:.1f}" if max_v > 0 else "",
    )
    svg += (
        '<text x="%.1f" y="%.1f" fill="%s" font-size="9" font-family="'
        'monospace" font-weight="600" text-anchor="end">%.1f tok/s</text>'
    ) % (width - pad_x, height - 4, accent, samples[-1])

    svg += "</svg>"
    return svg


# ──────────────────────────────────────────────────────────────────────────
# Format helpers
# ──────────────────────────────────────────────────────────────────────────

def _fmt_count(n: int) -> str:
    """Format a count with commas (e.g. 12482 → '12,482')."""
    if n == 0:
        return "\u2014"
    return f"{n:,}"


def _fmt_speed(v: float) -> str:
    """Format a tokens/sec value; show \u2014 for unmeasured."""
    if v is None or v <= 0:
        return "\u2014"
    return f"{v:.1f}"


def _fmt_ms(v: float | None) -> str:
    """Format a milliseconds value; show \u2014 for unmeasured."""
    if v is None or v <= 0:
        return "\u2014"
    if v < 10:
        return f"{v:.1f}"
    return f"{int(v)}"


def _fmt_since(ts_iso: str | None) -> str:
    """Format an ISO timestamp as 'Xs ago' or empty."""
    if not ts_iso:
        return ""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(ts_iso)
        delta = time.time() - dt.timestamp()
        if delta < 1:
            return "just now"
        return f"{int(delta)}s ago"
    except Exception:
        return ""


def _global_host_state(status: dict[str, Any]) -> str:
    """Derive a short host-state label and colour."""
    if status.get("degraded"):
        return "Degraded"
    lanes = status.get("lanes", {})
    states = set(
        _derive_display_state(l.get("lane_state", "offline"))
        for l in lanes.values()
    )
    if "offline" in states:
        return "Offline"
    if "waking" in states:
        return "Starting"
    if "degraded" in states:
        return "Degraded"
    return "Operational"


def _derive_display_state(raw: str) -> str:
    """Map internal lane state to display state."""
    return raw


def _status_pill_html(state: str) -> str:
    """Build the coloured status pill for a card."""
    colors = {
        "online": (_GREEN, "ONLINE"),
        "busy": (_AMBER, "BUSY"),
        "waking": (_AMBER, "WAKING"),
        "degraded": (_CORAL, "DEGRADED"),
        "offline": (_CORAL, "OFFLINE"),
    }
    color, label = colors.get(state, (_MUTED, state.upper()))
    return (
        f'<span style="display: inline-flex; align-items: center; gap: 4px; '
        f'padding: 2px 10px; border-radius: 10px; font-size: 0.7em; '
        f'font-weight: 600; font-family: sans-serif; '
        f'letter-spacing: 0.04em; '
        f'background: {color}20; color: {color}; border: 1px solid {color}40;">'
        f'<span style="width: 6px; height: 6px; border-radius: 50%; '
        f'background: {color};"></span>{label}</span>'
    )


# ──────────────────────────────────────────────────────────────────────────
# Card builders
# ──────────────────────────────────────────────────────────────────────────

def _build_card_html(
    lane_key: str,
    info: dict[str, Any],
    frames: list[dict[str, Any]],
    accent: str,
    bright: str,
    glow: str,
) -> str:
    """Build the full HTML for one lane card."""
    state = info.get("lane_state", "offline")
    display_state = _derive_display_state(state)
    is_online = state == "online"

    model = info.get("model", "")
    # Short display name: extract "LFM2.5 · 350M · Q6_K" from filename
    short_model = _short_model_name(model)
    ctx = info.get("ctx", 0)
    ctx_fmt = f"{ctx:,}" if ctx else "\u2014"

    # Metrics
    total_prompt = _fmt_count(info.get("total_prompt_tokens", 0))
    total_completion = _fmt_count(info.get("total_completion_tokens", 0))
    fastest = _fmt_speed(info.get("quickest_generation_tokens_per_second", 0.0))
    slowest = _fmt_speed(info.get("slowest_generation_tokens_per_second", 0.0))

    # Server-side timing from llama-server pipeline
    last_ttft = _fmt_ms(info.get("last_time_to_first_token_ms"))
    avg_ttft = _fmt_ms(info.get("avg_time_to_first_token_ms"))

    total_req = info.get("total_requests", 0)
    success_rate = info.get("success_rate", 100.0)
    last_time = _fmt_since(info.get("last_request_time"))
    last_success = info.get("last_success", True)

    # Sparkline
    speed_values = [f.get("generation_tokens_per_second", 0) for f in frames]
    sparkline = _build_sparkline(speed_values, accent, state)

    # Footer
    if total_req == 0:
        footer = (
            '<span style="color: %s;">Waiting for first inference</span>'
        ) % _MUTED
    else:
        footer_parts = []
        footer_parts.append(
            '<span style="color: %s;">%s request%s</span>' %
            (_SECONDARY, total_req, "s" if total_req != 1 else "")
        )
        if last_time:
            footer_parts.append(
                '<span style="color: %s;">Active %s</span>' %
                (_SECONDARY, last_time)
            )
        footer_parts.append(
            '<span style="color: %s;">%s%% success</span>' %
            (_GREEN if last_success else _CORAL, success_rate)
        )
        footer = " \u00b7 ".join(footer_parts)

    # Full model filename as tooltip
    model_tooltip = model or ""

    return f"""\
<div style="background: linear-gradient(180deg, {_PANEL} 0%, {_RAISED} 100%);
     border: 1px solid {_BORDER};
     border-radius: 20px;
     padding: 24px 26px;
     min-height: 380px;
     position: relative;
     overflow: hidden;
     box-shadow: 0 4px 24px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.04);
     font-family: sans-serif;">
  <!-- Top glow -->
  <div style="position: absolute; top: -40px; left: 50%; transform: translateX(-50%);
       width: 180px; height: 80px; border-radius: 50%;
       background: {glow}; filter: blur(24px); pointer-events: none;"></div>

  <!-- Card header -->
  <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px;">
    <div>
      <div style="font-size: 1.05em; font-weight: 700; color: {_PRIMARY};
           letter-spacing: 0.03em; font-family: sans-serif;">
        {lane_key.upper()}</div>
      <div style="font-size: 0.78em; color: {_SECONDARY}; margin-top: 2px;
           font-family: sans-serif;">
        {'Fast Response Lane' if lane_key == 'microbrain' else 'Reasoning Lane'}</div>
    </div>
    {_status_pill_html(state)}
  </div>

  <!-- Model identity -->
  <div style="margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid {_BORDER};">
    <div style="font-size: 0.85em; font-weight: 600; color: {bright};
         font-family: monospace;" title="{model_tooltip}">
      {short_model}</div>
    <div style="font-size: 0.75em; color: {_MUTED}; margin-top: 3px;
         font-family: monospace;">
      Context {ctx_fmt} \u00b7 <span title="{model_tooltip}" style="cursor: help; border-bottom: 1px dotted {_MUTED};">{model}</span>
    </div>
  </div>

  <!-- 2\u00d72 metric grid (spec \u00a76) -->
  <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px 16px; margin-bottom: 14px;">
    <div>
      <div style="font-size: 0.65em; color: {_MUTED}; letter-spacing: 0.06em;
           font-weight: 600; font-family: sans-serif; text-transform: uppercase;">
        Tokens In</div>
      <div style="font-size: 1.4em; font-weight: 700; color: {_PRIMARY};
           font-family: monospace; line-height: 1.3;">{total_prompt}</div>
      <div style="font-size: 0.6em; color: {_MUTED};">Since restart</div>
    </div>
    <div>
      <div style="font-size: 0.65em; color: {_MUTED}; letter-spacing: 0.06em;
           font-weight: 600; font-family: sans-serif; text-transform: uppercase;">
        Tokens Out</div>
      <div style="font-size: 1.4em; font-weight: 700; color: {_PRIMARY};
           font-family: monospace; line-height: 1.3;">{total_completion}</div>
      <div style="font-size: 0.6em; color: {_MUTED};">Since restart</div>
    </div>
    <div>
      <div style="font-size: 0.65em; color: {_MUTED}; letter-spacing: 0.06em;
           font-weight: 600; font-family: sans-serif; text-transform: uppercase;">
        Fastest</div>
      <div style="font-size: 1.3em; font-weight: 700; color: {accent};
           font-family: monospace; line-height: 1.3;">{fastest}</div>
      <div style="font-size: 0.6em; color: {_MUTED};">tokens/sec</div>
    </div>
    <div>
      <div style="font-size: 0.65em; color: {_MUTED}; letter-spacing: 0.06em;
           font-weight: 600; font-family: sans-serif; text-transform: uppercase;">
        Slowest</div>
      <div style="font-size: 1.3em; font-weight: 700; color: {accent};
           font-family: monospace; line-height: 1.3;">{slowest}</div>
      <div style="font-size: 0.6em; color: {_MUTED};">tokens/sec</div>
    </div>
  </div>

  <!-- Server-side timing row (spec \u00a79 \u2014 llama-server pipeline) -->
  <div style="display: flex; gap: 20px; margin-bottom: 12px; padding: 8px 0;
       border-bottom: 1px solid {_BORDER};">
    <div>
      <div style="font-size: 0.6em; color: {_MUTED}; letter-spacing: 0.06em;
           font-weight: 600; font-family: sans-serif; text-transform: uppercase;">
        TTFT \u2014 Last</div>
      <div style="font-size: 1.1em; font-weight: 700; color: {accent};
           font-family: monospace; line-height: 1.3;">{last_ttft}</div>
      <div style="font-size: 0.6em; color: {_MUTED};">ms (server-side)</div>
    </div>
    <div>
      <div style="font-size: 0.6em; color: {_MUTED}; letter-spacing: 0.06em;
           font-weight: 600; font-family: sans-serif; text-transform: uppercase;">
        TTFT \u2014 Avg</div>
      <div style="font-size: 1.1em; font-weight: 700; color: {accent};
           font-family: monospace; line-height: 1.3;">{avg_ttft}</div>
      <div style="font-size: 0.6em; color: {_MUTED};">ms (server-side)</div>
    </div>
  </div>

  <!-- Sparkline (spec \u00a77) -->
  <div style="margin-bottom: 10px;">
    <div style="font-size: 0.6em; color: {_MUTED}; letter-spacing: 0.06em;
         font-weight: 600; font-family: sans-serif; text-transform: uppercase;
         margin-bottom: 2px;">
      Recent Generation Speed</div>
    {sparkline}
  </div>

  <!-- Footer (spec \u00a77) -->
  <div style="font-size: 0.7em; padding-top: 8px; border-top: 1px solid {_BORDER};
       display: flex; justify-content: space-between; align-items: center;">
    {footer}
  </div>
</div>"""


def _short_model_name(filename: str) -> str:
    """Convert a GGUF filename to a short readable label.

    'LFM2.5-350M-Q6_K.gguf' → 'LFM2.5 · 350M · Q6_K'
    'LFM2.5-1.2B-Instruct-Q6_K.gguf' → 'LFM2.5 Instruct · 1.2B · Q6_K'
    """
    if not filename:
        return "\u2014"
    name = filename.replace(".gguf", "")
    parts = name.split("-")

    # Try to parse model family and size
    if len(parts) >= 2:
        family = parts[0]
        # Check for Instruct variant
        instruct = ""
        size_candidates = []
        other_parts = []
        for p in parts[1:]:
            if p in ("Instruct", "Chat", "Base"):
                instruct = p
            elif any(c in p for c in ("B", "M")) and any(
                c.isdigit() for c in p
            ):
                size_candidates.append(p)
            else:
                other_parts.append(p)

        # Simplify: LFM2.5-350M-Q6_K → "LFM2.5 · 350M · Q6_K"
        result_parts = [family]
        if instruct:
            result_parts.append(instruct)
        if size_candidates:
            result_parts.append(size_candidates[0])
        if other_parts:
            result_parts.append(other_parts[0])

        return " \u00b7 ".join(result_parts)
    return name


# ──────────────────────────────────────────────────────────────────────────
# Dashboard builder
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class DashboardTemplate:
    """Strings and a refresh callable -- safe to create outside Blocks."""
    header_html: str
    status_html: str
    micro_html: str
    main_html: str
    refresh_fn: Callable[[], tuple[str, str, str]]
    refresh_seconds: int


def _build_header_html() -> str:
    """Static brand header with glowing brain badge."""
    return f"""\
<style>
  @media (prefers-reduced-motion: no-preference) {{
    @keyframes brain-pulse {{
      0%, 100% {{ box-shadow: 0 0 12px rgba(244,114,182,0.15), 0 0 28px rgba(244,114,182,0.06); }}
      50% {{ box-shadow: 0 0 18px rgba(244,114,182,0.30), 0 0 40px rgba(244,114,182,0.12); }}
    }}
    .brain-badge {{ animation: brain-pulse 3s ease-in-out infinite; }}
  }}
  .brain-badge {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 56px; height: 56px; border-radius: 16px;
    background: linear-gradient(135deg, rgba(244,114,182,0.12) 0%, rgba(139,92,246,0.08) 100%);
    border: 1px solid rgba(244,114,182,0.20);
    font-size: 30px; line-height: 1;
    margin-bottom: 6px;
    transition: box-shadow 0.4s ease;
  }}
</style>
<div style="text-align: center; padding: 28px 20px 6px;">
  <div class="brain-badge">\U0001f9e0</div>
  <h1 style="margin: 4px 0 0; font-size: 1.7em; font-weight: 700;
      color: {_PRIMARY}; letter-spacing: 0.04em;
      font-family: sans-serif;">
    ASHAT NEURAL HOST</h1>
  <p style="color: {_SECONDARY}; font-size: 0.82em; margin: 2px 0 0;
      font-family: sans-serif; letter-spacing: 0.02em;">
    Private Neural Inference \u00b7 Public Telemetry</p>
</div>"""


def _build_status_row_html(snapshot: PublicSnapshot) -> str:
    """Build the global status row below the header."""
    status = snapshot.render_status()
    host_state = _global_host_state(status)

    state_colors = {
        "Operational": _GREEN,
        "Starting": _AMBER,
        "Degraded": _AMBER,
        "Offline": _CORAL,
    }
    dot_color = state_colors.get(host_state, _MUTED)

    lanes = status.get("lanes", {})
    online_count = sum(
        1 for l in lanes.values() if l.get("lane_state") == "online"
    )
    total_count = len(lanes)

    # Compute seconds since last refresh
    last_refresh = _fmt_since(
        max(
            (
                l.get("last_request_time")
                for l in lanes.values()
                if l.get("last_request_time")
            ),
            default=None,
        )
    )

    return f"""\
<div style="text-align: center; padding: 6px 20px 20px;">
  <span style="display: inline-flex; align-items: center; gap: 6px;
       font-size: 0.8em; font-family: sans-serif; color: {_SECONDARY};">
    <span style="width: 7px; height: 7px; border-radius: 50%;
         background: {dot_color};"></span>
    <span style="font-weight: 600; color: {dot_color};">{host_state}</span>
    <span style="color: {_MUTED};">\u00b7</span>
    <span>{online_count}/{total_count} lanes online</span>
    <span style="color: {_MUTED};">\u00b7</span>
    <span>Updated {last_refresh or 'just now'}</span>
  </span>
</div>"""


def _build_cards_html(snapshot: PublicSnapshot) -> tuple[str, str]:
    """Build both lane-card HTML strings.

    Returns:
        (microbrain_html, mainbrain_html)
    """
    status = snapshot.render_status()
    frames = snapshot.render_frames()
    lanes = status.get("lanes", {})

    mb_info = lanes.get("microbrain", {})
    mm_info = lanes.get("mainbrain", {})

    mb_frames = frames.get("microbrain", [])
    mm_frames = frames.get("mainbrain", [])

    micro_html = _build_card_html(
        "microbrain", mb_info, mb_frames,
        _MICRO_ACCENT, _MICRO_BRIGHT, _MICRO_GLOW,
    )
    main_html = _build_card_html(
        "mainbrain", mm_info, mm_frames,
        _MAIN_ACCENT, _MAIN_BRIGHT, _MAIN_GLOW,
    )
    return micro_html, main_html


def _build_footer_html() -> str:
    """Static footer bar."""
    return f"""\
<div style="text-align: center; padding: 16px 20px 24px;">
  <span style="font-size: 0.68em; color: {_MUTED}; font-family: sans-serif;
       letter-spacing: 0.03em;">
    Private inference lanes \u00b7 Public telemetry only</span>
</div>"""


def build_dashboard(
    snapshot_provider: Callable[[], PublicSnapshot],
    refresh_seconds: int = 8,
) -> DashboardTemplate:
    """Create the full dashboard within a Gradio Blocks context.

    Usage in ``app.py``::

        dashboard = build_dashboard(snapshot_provider, refresh_seconds)
        # \u2026 inside ``with gr.Blocks() as demo:`` \u2026
        dashboard.header.render()
        dashboard.status_row.render()
        with gr.Row(equal_height=True):
            with gr.Column(scale=1, min_width=320):
                dashboard.micro_card.render()
            with gr.Column(scale=1, min_width=320):
                dashboard.main_card.render()
    """
    initial_snapshot = snapshot_provider()

    header_html = _build_header_html()
    status_html = _build_status_row_html(initial_snapshot)
    micro_html, main_html = _build_cards_html(initial_snapshot)

    def _refresh() -> tuple[str, str, str]:
        """Called by gr.Timer on each tick."""
        snap = snapshot_provider()
        status = _build_status_row_html(snap)
        micro, main = _build_cards_html(snap)
        return status, micro, main

    return DashboardTemplate(
        header_html=header_html,
        status_html=status_html,
        micro_html=micro_html,
        main_html=main_html,
        refresh_fn=_refresh,
        refresh_seconds=refresh_seconds,
    )
