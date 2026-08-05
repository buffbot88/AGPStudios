"""Ashat Neural Network — server-rendered public-telemetry dashboard.

Server-rendered public telemetry dashboard for the Oracle BrainStem host.
The page is pure FastAPI HTML with a small browser polling loop.

``render_index_html(snapshot_provider, refresh_seconds)`` returns a
self-contained ``<!DOCTYPE html>`` document that:

  * Shows the operator-facing header, status row, single BrainStem
    neural-lane card, and footer (server-rendered on first paint so
    the page is meaningful before the first poll lands).
  * Embeds a tiny JavaScript ``setInterval`` that polls
    ``GET /api/dashboard_html`` every ``refresh_seconds`` and replaces
    the status + brainstem-card ``innerHTML`` in place. This mirrors
    the previous timer-driven refresh behaviour with plain browser
    fetch; no authentication is required for public telemetry.

``render_dashboard_html_json(snapshot)`` is the companion endpoint
payload -- returns the ``status_html`` + ``brainstem_html`` strings
that the JS poll swaps in.

The CSS, responsive layout, status pill, sparkline (inline SVG), and
BrainStem card markup provide the public telemetry surface.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from html import escape
from typing import Any

from public_snapshot import (
    DIAGNOSTIC_PILL_OVERRIDES,
    PUBLIC_ERROR_MESSAGES,
    PublicSnapshot,
)


# ──────────────────────────────────────────────────────────────────────────
# Constants — Ashat Hub design tokens
# ──────────────────────────────────────────────────────────────────────────

# Background & Surface
_BG = "#0b0b0c"  # Deep Obsidian / Near Black
_CARD_BG = "#121316"  # Surface Cards
_BORDER = "#27272a"  # Subtle border separation

# Text
_PRIMARY = "#f4f4f5"  # Soft Off-White
_SECONDARY = "#9ca3af"  # Cool Gray (muted)
_MUTED = "#6b7280"  # Even more muted

# Accent
_ACCENT = "#f97316"  # Vibrant Burnished Orange
_ACCENT_GLOW = "rgba(249, 115, 22, 0.15)"

# Status Colors
_GREEN = "#22c55e"  # Emerald Green (operational)
_AMBER = "#f59e0b"  # Amber (warnings)
_CORAL = "#ef4444"  # Red (errors/offline)

# Legacy aliases for compatibility
_PANEL = _CARD_BG
_RAISED = "#1a1b1e"
_BRIGHT = _PRIMARY
_GLOW = _ACCENT_GLOW


# ──────────────────────────────────────────────────────────────────────────
# Sparkline — inline SVG
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
    """Format a tokens/sec value; show — for unmeasured."""
    if v is None or v <= 0:
        return "\u2014"
    return f"{v:.1f}"


def _fmt_ms(v: float | None) -> str:
    """Format a milliseconds value; show — for unmeasured."""
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
    states = set(l.get("lane_state", "offline") for l in lanes.values())
    if "offline" in states:
        return "Offline"
    if "waking" in states:
        return "Starting"
    if "degraded" in states:
        return "Degraded"
    return "Operational"


def _status_pill_html(
    state: str,
    *,
    override: tuple[str, str] | None = None,
) -> str:
    """Build the coloured status pill for a card — Ashat Hub capsule style."""
    if override is not None:
        color, label = override
    else:
        colors = {
            "online": (_GREEN, "ONLINE"),
            "busy": (_AMBER, "BUSY"),
            "waking": (_AMBER, "WAKING"),
            "degraded": (_CORAL, "DEGRADED"),
            "offline": (_CORAL, "OFFLINE"),
        }
        color, label = colors.get(state, (_MUTED, state.upper()))
    safe_label = escape(str(label))
    return (
        f'<span style="display: inline-flex; align-items: center; gap: 6px; '
        f'padding: 4px 12px; border-radius: 9999px; font-size: 0.7em; '
        f'font-weight: 600; font-family: Inter, sans-serif; '
        f'letter-spacing: 0.05em; text-transform: uppercase; '
        f'background: {_CARD_BG}; color: {color}; border: 1px solid {_BORDER};">'
        f'<span style="width: 6px; height: 6px; border-radius: 50%; '
        f'background: {color}; box-shadow: 0 0 6px {color}80;"></span>{safe_label}</span>'
    )


# ──────────────────────────────────────────────────────────────────────────
# Card builder — single BrainStem lane (Ashat Hub sleek card style)
# ──────────────────────────────────────────────────────────────────────────

def _build_card_html(
    lane_key: str,
    info: dict[str, Any],
    frames: list[dict[str, Any]],
    accent: str,
    bright: str,
    glow: str,
) -> str:
    """Build the full HTML for the single BrainStem lane card."""
    state = info.get("lane_state", "offline")
    model = info.get("model", "")
    short_model = _short_model_name(model)
    ctx = info.get("ctx", 0)
    ctx_fmt = f"{ctx:,}" if ctx else "\u2014"
    lane_display = escape(str(lane_key or ""))

    last_failure_code: str | None = info.get("last_failure_code")
    reason_message: str | None = info.get("reason_message")
    override_pill: tuple[str, str] | None = (
        DIAGNOSTIC_PILL_OVERRIDES.get(last_failure_code)
        if last_failure_code
        else None
    )

    total_prompt = _fmt_count(info.get("total_prompt_tokens", 0))
    total_completion = _fmt_count(info.get("total_completion_tokens", 0))
    fastest = _fmt_speed(info.get("quickest_generation_tokens_per_second", 0.0))
    slowest = _fmt_speed(info.get("slowest_generation_tokens_per_second", 0.0))

    last_ttft = _fmt_ms(info.get("last_time_to_first_token_ms"))
    avg_ttft = _fmt_ms(info.get("avg_time_to_first_token_ms"))

    total_req = info.get("total_requests", 0)
    success_rate = info.get("success_rate", 100.0)
    last_time = _fmt_since(info.get("last_request_time"))
    last_success = info.get("last_success", True)

    speed_values = [f.get("generation_tokens_per_second", 0) for f in frames]
    sparkline = _build_sparkline(speed_values, accent, state)

    if total_req == 0:
        footer = (
            '<span style="color: %s;">Waiting for first inference</span>'
        ) % _MUTED
    else:
        footer_parts = [
            '<span style="color: %s;">%s request%s</span>' % (
                _SECONDARY, total_req, "s" if total_req != 1 else ""
            )
        ]
        if last_time:
            footer_parts.append(
                '<span style="color: %s;">Active %s</span>'
                % (_SECONDARY, last_time)
            )
        footer_parts.append(
            '<span style="color: %s;">%s%% success</span>'
            % (_GREEN if last_success else _CORAL, success_rate)
        )
        footer = " \u00b7 ".join(footer_parts)

    model_display = escape(str(model or ""))
    model_tooltip = escape(str(model or ""), quote=True)
    reason_display = escape(str(reason_message or ""))

    safe_failure_code = escape(
        str(last_failure_code or "").replace("_", " ").title()
    )
    diagnostic_html = ""
    if last_failure_code and reason_message:
        diag_color, _ = override_pill if override_pill else (_CORAL, "")
        diagnostic_html = (
            f'<div style="margin: 0 0 16px; padding: 12px 14px; '
            f'border: 1px solid {diag_color}66; border-radius: 10px; '
            f'background: {diag_color}14; color: {diag_color}; '
            f'font-size: 0.78em; line-height: 1.4; '
            f'font-family: Inter, sans-serif;">'
            f'<div style="font-weight: 700; letter-spacing: 0.04em; '
            f'margin-bottom: 4px; font-size: 0.82em;">'
            f'\u26a0  {safe_failure_code}'
            f'</div>'
            f'<div style="color: {_PRIMARY}; opacity: 0.92;">'
            f'{reason_display}'
            f'</div>'
            f'</div>'
        )

    return f"""\
<article class="lane-card" style="background: {_CARD_BG};
     border: 1px solid {_BORDER};
     border-radius: 12px;
     padding: 24px;
     min-height: 360px;
     position: relative;
     overflow: hidden;
     font-family: Inter, sans-serif;">

  <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px;">
    <div>
      <div style="font-size: 1.1em; font-weight: 700; color: {_PRIMARY};
           letter-spacing: 0.02em; font-family: Inter, sans-serif;">
        {lane_display.upper()}</div>
      <div style="font-size: 0.78em; color: {_SECONDARY}; margin-top: 4px;
           font-family: Inter, sans-serif;">
        Primary Inference Lane</div>
    </div>
    {_status_pill_html(state, override=override_pill)}
  </div>

  {diagnostic_html}

  <div style="margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid {_BORDER};">
    <div style="font-size: 0.88em; font-weight: 600; color: {_PRIMARY};
         font-family: 'JetBrains Mono', monospace;" title="{model_tooltip}">
      {escape(short_model)}</div>
    <div style="font-size: 0.75em; color: {_MUTED}; margin-top: 4px;
         font-family: 'JetBrains Mono', monospace;">
      Context {ctx_fmt} \u00b7 <span title="{model_tooltip}" style="cursor: help; border-bottom: 1px dotted {_MUTED};">{model_display}</span>
    </div>
  </div>

  <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px;">
    <div>
      <div style="font-size: 0.65em; color: {_MUTED}; letter-spacing: 0.08em;
           font-weight: 600; font-family: Inter, sans-serif; text-transform: uppercase;">
        Tokens In</div>
      <div style="font-size: 1.5em; font-weight: 700; color: {_PRIMARY};
           font-family: 'JetBrains Mono', monospace; line-height: 1.3;">{total_prompt}</div>
      <div style="font-size: 0.6em; color: {_MUTED};">Since restart</div>
    </div>
    <div>
      <div style="font-size: 0.65em; color: {_MUTED}; letter-spacing: 0.08em;
           font-weight: 600; font-family: Inter, sans-serif; text-transform: uppercase;">
        Tokens Out</div>
      <div style="font-size: 1.5em; font-weight: 700; color: {_PRIMARY};
           font-family: 'JetBrains Mono', monospace; line-height: 1.3;">{total_completion}</div>
      <div style="font-size: 0.6em; color: {_MUTED};">Since restart</div>
    </div>
    <div>
      <div style="font-size: 0.65em; color: {_MUTED}; letter-spacing: 0.08em;
           font-weight: 600; font-family: Inter, sans-serif; text-transform: uppercase;">
        Fastest</div>
      <div style="font-size: 1.3em; font-weight: 700; color: {accent};
           font-family: 'JetBrains Mono', monospace; line-height: 1.3;">{fastest}</div>
      <div style="font-size: 0.6em; color: {_MUTED};">tokens/sec</div>
    </div>
    <div>
      <div style="font-size: 0.65em; color: {_MUTED}; letter-spacing: 0.08em;
           font-weight: 600; font-family: Inter, sans-serif; text-transform: uppercase;">
        Slowest</div>
      <div style="font-size: 1.3em; font-weight: 700; color: {accent};
           font-family: 'JetBrains Mono', monospace; line-height: 1.3;">{slowest}</div>
      <div style="font-size: 0.6em; color: {_MUTED};">tokens/sec</div>
    </div>
  </div>

  <div style="display: flex; gap: 24px; margin-bottom: 12px; padding: 10px 0;
       border-bottom: 1px solid {_BORDER};">
    <div>
      <div style="font-size: 0.6em; color: {_MUTED}; letter-spacing: 0.08em;
           font-weight: 600; font-family: Inter, sans-serif; text-transform: uppercase;">
        TTFT \u2014 Last</div>
      <div style="font-size: 1.1em; font-weight: 700; color: {accent};
           font-family: 'JetBrains Mono', monospace; line-height: 1.3;">{last_ttft}</div>
      <div style="font-size: 0.6em; color: {_MUTED};">ms (server-side)</div>
    </div>
    <div>
      <div style="font-size: 0.6em; color: {_MUTED}; letter-spacing: 0.08em;
           font-weight: 600; font-family: Inter, sans-serif; text-transform: uppercase;">
        TTFT \u2014 Avg</div>
      <div style="font-size: 1.1em; font-weight: 700; color: {accent};
           font-family: 'JetBrains Mono', monospace; line-height: 1.3;">{avg_ttft}</div>
      <div style="font-size: 0.6em; color: {_MUTED};">ms (server-side)</div>
    </div>
  </div>

  <div style="margin-bottom: 10px;">
    <div style="font-size: 0.6em; color: {_MUTED}; letter-spacing: 0.08em;
         font-weight: 600; font-family: Inter, sans-serif; text-transform: uppercase;
         margin-bottom: 4px;">
      Recent Generation Speed</div>
    {sparkline}
  </div>

  <div style="font-size: 0.7em; padding-top: 10px; border-top: 1px solid {_BORDER};
       display: flex; justify-content: space-between; align-items: center;">
    {footer}
  </div>
</article>"""


def _short_model_name(filename: str) -> str:
    """Convert a GGUF filename to a short readable label."""
    if not filename:
        return "\u2014"
    name = filename.replace(".gguf", "")
    parts = name.split("-")
    if len(parts) >= 2:
        family = parts[0]
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
# Section builders — header, status row, cards, footer
# ──────────────────────────────────────────────────────────────────────────

def _build_header_html() -> str:
    """Build the Ashat Hub header with backdrop blur navigation."""
    return f"""\
<header class="hub-header">
  <nav class="hub-nav" aria-label="Primary navigation">
    <div class="nav-left">
      <a class="hub-brand" href="https://agpstudios.org" rel="noopener">
        <span class="brand-mark" aria-hidden="true"><img src="/images/lion-logo-32.png" alt="ASHAT Hub"></span>
        <span>ASHAT<span class="hub-accent">Hub</span></span>
      </a>
      <span class="version-badge">v5.8</span>
    </div>
    <div class="nav-links">
      <a class="hub-link" href="https://agpstudios.org/chat" rel="noopener">Chat</a>
      <a class="hub-link" href="https://agpstudios.org/community" rel="noopener">Community</a>
      <a class="hub-link" href="https://agpstudios.org/docs" rel="noopener">Documentation</a>
      <a class="hub-link" href="https://agpstudios.org/support" rel="noopener">Support</a>
    </div>
    <div class="nav-right">
      <span class="status-pill">
        <span class="status-dot"></span>
        Operational
      </span>
    </div>
  </nav>
</header>"""


def _build_status_row_html(snapshot: PublicSnapshot) -> str:
    """Build the global status row below the header."""
    status = snapshot.render_status()
    host_state = _global_host_state(status)

    state_colors = {
        "Operational": _GREEN,
        "Starting": _AMBER,
        "Degraded": _CORAL,
        "Offline": _MUTED,
    }
    dot_color = state_colors.get(host_state, _MUTED)

    lanes = status.get("lanes", {})
    online_count = sum(
        1 for l in lanes.values() if l.get("lane_state") == "online"
    )
    total_count = len(lanes)

    last_failure_codes = [
        l.get("last_failure_code") for l in lanes.values()
        if l.get("last_failure_code")
    ]
    priority_order = (
        "LOCAL_MODEL_UNAVAILABLE",
        "BINARY_INSTALL_FAILED",
    )
    headline_code: str | None = None
    for code in priority_order:
        if code in last_failure_codes:
            headline_code = code
            break
    headline_msg = (
        PUBLIC_ERROR_MESSAGES.get(headline_code) if headline_code else None
    )

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

    # Queue info
    queue = status.get("queue", {})
    queue_depth = queue.get("depth", 0)
    queue_limit = queue.get("limit", 16)
    queue_color = _ACCENT if queue_depth > 0 else _MUTED
    queue_html = (
        f'<span style="color: {_MUTED};">\u00b7</span>'
        f'<span style="color: {queue_color};">Queue: {queue_depth}/{queue_limit}</span>'
    ) if queue_depth > 0 else ""

    headline_html = ""
    if headline_code and headline_msg:
        headline_html = (
            f'<div style="margin: 6px 20px 0; text-align: center; '
            f'font-family: Inter, sans-serif; font-size: 0.78em; color: {_AMBER};">'
            f'\u26a0 <span style="font-weight: 600;">{escape(headline_code.replace("_", " ").title())}</span>'
            f' \u00b7 <span>{headline_msg}</span>'
            f'</div>'
        )

    return f"""\
<div class="status-strip" style="display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; padding: 0 0 24px; color: {_MUTED}; font-size: 0.78em; font-family: Inter, sans-serif;">
  <span style="display: inline-flex; align-items: center; gap: 8px;">
    <span style="width: 7px; height: 7px; border-radius: 50%; background: {dot_color}; box-shadow: 0 0 8px {dot_color}80;"></span>
    <span style="font-weight: 650; color: {dot_color};">{host_state}</span>
    <span>{online_count}/{total_count} lanes ready</span>
  </span>
  <span style="display: inline-flex; align-items: center; gap: 8px; color: {_MUTED};">
    <span>Updated {last_refresh or 'just now'}</span>{queue_html}
  </span>
</div>{headline_html}"""


def _build_cards_html(snapshot: PublicSnapshot) -> str:
    """Build the single BrainStem lane card HTML for one snapshot."""
    status = snapshot.render_status()
    frames = snapshot.render_frames()
    lanes = status.get("lanes", {})

    bs_info = lanes.get("brainstem", {})
    bs_frames = frames.get("brainstem", [])

    return _build_card_html(
        "brainstem", bs_info, bs_frames,
        _ACCENT, _BRIGHT, _GLOW,
    )


def _build_footer_html() -> str:
    """Build the Ashat Hub footer."""
    return f"""\
<footer class="hub-footer">
  <div class="footer-rule"></div>
  <div class="footer-nav-row">
    <a class="footer-brand" href="https://agpstudios.org" rel="noopener">
      <span class="brand-mark" aria-hidden="true"><img src="/images/lion-logo-32.png" alt="ASHAT Hub"></span>
      <span>ASHAT<span class="hub-accent">Hub</span></span>
    </a>
    <div class="footer-links">
      <a class="footer-link" href="https://agpstudios.org/chat" rel="noopener">Chat</a>
      <a class="footer-link" href="https://agpstudios.org/docs" rel="noopener">Docs</a>
      <a class="footer-link" href="https://agpstudios.org/community" rel="noopener">Community</a>
      <a class="footer-link" href="https://agpstudios.org/terms" rel="noopener">Terms</a>
      <a class="footer-link" href="https://agpstudios.org/privacy" rel="noopener">Privacy</a>
    </div>
  </div>
  <div class="footer-copyright">
    AGP Studios, Inc. \u00b7 &copy; 2026 \u00b7 ASHAT Hub \u00b7 v5.8 \u00b7 All rights reserved.
  </div>
</footer>"""


# ──────────────────────────────────────────────────────────────────────────
# Public rendering entry points — server-rendered HTML fragment,
#    companion JSON payload used by the browser polling loop.
# ──────────────────────────────────────────────────────────────────────────


def render_dashboard_html_json(snapshot: PublicSnapshot) -> dict[str, str]:
    """Return the JSON payload the client polls to refresh the page.

    Used by ``GET /api/dashboard_html``. Returns pre-rendered HTML
    snippets so the styling logic lives in one place (this module)
    rather than being duplicated in client JavaScript.
    """
    return {
        "status_html": _build_status_row_html(snapshot),
        "brainstem_html": _build_cards_html(snapshot),
    }


def render_index_html(
    snapshot_provider: Callable[[], PublicSnapshot],
    refresh_seconds: int = 8,
) -> str:
    """Render the public telemetry dashboard as a standalone HTML document.

    Returns a complete ``<!DOCTYPE html>`` page with embedded CSS, the
    server-rendered status row + BrainStem lane card, a Plotly time-series
    chart (generation speed + latency), and a JavaScript ``setInterval``
    polling loop that fetches ``/api/dashboard_html`` every
    ``refresh_seconds`` and swaps the innerHTML of the status and
    brainstem container divs in place, plus updates the Plotly chart
    from ``/api/dashboard_timeseries``.

    The first paint is server-rendered so the page is meaningful before
    the first poll lands. The page is pure HTML.
    """
    safe_refresh = max(1, int(refresh_seconds))
    initial_snapshot = snapshot_provider()

    header_html = _build_header_html()
    status_html = _build_status_row_html(initial_snapshot)
    brainstem_html = _build_cards_html(initial_snapshot)
    footer_html = _build_footer_html()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ASHAT Hub \u00b7 Neural Host Telemetry</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">

<style>
  /* ═══════════════════════════════════════════════════════════════════
     ASHAT HUB DESIGN TOKENS
     ═══════════════════════════════════════════════════════════════════ */
  :root {{
    --bg-primary: {_BG};
    --bg-card: {_CARD_BG};
    --border-color: {_BORDER};
    --text-primary: {_PRIMARY};
    --text-muted: {_SECONDARY};
    --accent: {_ACCENT};
    --success: {_GREEN};
  }}

  *, *::before, *::after {{ box-sizing: border-box; }}
  :root {{ color-scheme: dark; }}

  body {{
    background-color: var(--bg-primary);
    color: var(--text-primary);
    margin: 0;
    padding: 0;
    min-height: 100vh;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }}

  ::selection {{ background: rgba(249, 115, 22, 0.3); color: var(--text-primary); }}

  a {{ color: var(--text-primary); text-decoration: none; transition: color 0.2s ease; }}
  a:hover {{ color: var(--accent); }}

  /* ═══════════════════════════════════════════════════════════════════
     NAVIGATION — Backdrop blur, fixed top bar
     ═══════════════════════════════════════════════════════════════════ */
  .hub-header {{
    position: sticky;
    top: 0;
    z-index: 100;
    background: rgba(11, 11, 12, 0.85);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border-color);
  }}

  .hub-nav {{
    max-width: 1200px;
    margin: 0 auto;
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 2rem;
    gap: 2rem;
  }}

  .nav-left, .nav-right {{
    display: flex;
    align-items: center;
    gap: 1rem;
  }}

  .hub-brand {{
    display: inline-flex;
    align-items: center;
    gap: 10px;
    font-size: 0.95rem;
    font-weight: 700;
    letter-spacing: 0.02em;
    color: var(--text-primary);
  }}

  .hub-accent {{ color: var(--accent); font-weight: 700; }}

  .brand-mark {{
    width: 28px;
    height: 28px;
    display: grid;
    place-items: center;
    border-radius: 6px;
    overflow: hidden;
  }}

  .brand-mark img {{
    width: 100%;
    height: 100%;
    object-fit: contain;
  }}

  .hero-logo {{
    width: 120px;
    height: 120px;
    margin: 0 auto 2rem;
    border-radius: 16px;
    overflow: hidden;
    box-shadow: 0 0 40px rgba(249, 115, 22, 0.2);
  }}

  .hero-logo img {{
    width: 100%;
    height: 100%;
    object-fit: contain;
  }}

  .version-badge {{
    background: var(--bg-card);
    color: var(--text-muted);
    font-size: 0.7rem;
    padding: 4px 10px;
    border-radius: 6px;
    border: 1px solid var(--border-color);
    font-weight: 600;
    font-family: 'JetBrains Mono', monospace;
    letter-spacing: 0.03em;
  }}

  .nav-links {{
    display: flex;
    gap: 2rem;
    align-items: center;
  }}

  .hub-link {{
    color: var(--text-muted);
    font-size: 0.85rem;
    font-weight: 500;
    transition: color 0.2s ease;
    text-decoration: none;
  }}

  .hub-link:hover {{ color: var(--text-primary); }}

  .status-pill {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 6px 14px;
    border-radius: 9999px;
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--success);
  }}

  .status-dot {{
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--success);
    box-shadow: 0 0 8px var(--success);
    animation: pulse 2s infinite;
  }}

  @keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.6; }}
  }}

  /* ═══════════════════════════════════════════════════════════════════
     HERO SECTION — Editorial style with italic accent
     ═══════════════════════════════════════════════════════════════════ */
  .hero {{
    text-align: center;
    padding: 4rem 2rem 3rem;
    max-width: 800px;
    margin: 0 auto;
  }}

  .hero-badge {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 8px 16px;
    border-radius: 9999px;
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    font-size: 0.78rem;
    color: var(--text-muted);
    margin-bottom: 2rem;
  }}

  .hero-badge .live-dot {{
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--success);
    box-shadow: 0 0 6px var(--success);
  }}

  .hero h1 {{
    font-family: 'Inter', sans-serif;
    font-size: clamp(2.5rem, 6vw, 4rem);
    font-weight: 800;
    letter-spacing: -0.03em;
    line-height: 1.1;
    margin: 0 0 1.5rem 0;
    color: var(--text-primary);
  }}

  .hero h1 .accent {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-style: italic;
    color: var(--accent);
    font-weight: 400;
    font-size: 1.1em;
  }}

  .hero-subtitle {{
    font-size: 1.1rem;
    color: var(--text-muted);
    max-width: 600px;
    margin: 0 auto 2rem;
    line-height: 1.6;
  }}

  .hero-meta {{
    font-size: 0.85rem;
    color: var(--text-muted);
    letter-spacing: 0.02em;
  }}

  .hero-meta span {{
    margin: 0 0.5rem;
    opacity: 0.5;
  }}

  /* ═══════════════════════════════════════════════════════════════════
     CONTENT CONTAINER
     ═══════════════════════════════════════════════════════════════════ */
  .container {{
    max-width: 900px;
    margin: 0 auto;
    padding: 0 2rem;
  }}

  #status, #brainstem {{
    line-height: 1.4;
  }}



  /* ═══════════════════════════════════════════════════════════════════
     FOOTER
     ═══════════════════════════════════════════════════════════════════ */
  .hub-footer {{
    max-width: 900px;
    margin: 0 auto;
    padding: 1rem 2rem 3rem;
  }}

  .footer-rule {{
    height: 1px;
    background: var(--border-color);
    margin-bottom: 1.5rem;
  }}

  .footer-nav-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1.5rem;
  }}

  .footer-brand {{
    display: inline-flex;
    align-items: center;
    gap: 10px;
    font-size: 0.88rem;
    font-weight: 700;
    letter-spacing: 0.02em;
    text-decoration: none;
    color: var(--text-primary);
  }}

  .footer-links {{
    display: flex;
    gap: 1.5rem;
    align-items: center;
  }}

  .footer-link {{
    color: var(--text-muted);
    font-size: 0.8rem;
    font-weight: 500;
    transition: color 0.2s ease;
    text-decoration: none;
  }}

  .footer-link:hover {{ color: var(--text-primary); }}

  .footer-copyright {{
    text-align: center;
    color: var(--text-muted);
    font-size: 0.72rem;
    letter-spacing: 0.02em;
    padding-top: 1.5rem;
    border-top: 1px solid var(--border-color);
  }}

  /* ═══════════════════════════════════════════════════════════════════
     RESPONSIVE
     ═══════════════════════════════════════════════════════════════════ */
  @media (max-width: 768px) {{
    .hub-nav {{
      flex-wrap: wrap;
      padding: 1rem;
    }}

    .nav-links {{
      order: 3;
      width: 100%;
      justify-content: center;
      flex-wrap: wrap;
      gap: 1rem;
      padding-top: 1rem;
      border-top: 1px solid var(--border-color);
    }}

    .hero {{
      padding: 2rem 1rem;
    }}

    .container, .hub-footer {{
      padding-left: 1rem;
      padding-right: 1rem;
    }}

    .footer-nav-row {{
      flex-direction: column;
      gap: 1rem;
      text-align: center;
    }}

    .footer-links {{
      flex-wrap: wrap;
      justify-content: center;
      gap: 1rem;
    }}
  }}
</style>
</head>
<body>
{header_html}

<div class="hero">
  <div class="hero-logo">
    <img src="/images/lion-logo-128.png" alt="ASHAT Neural Host">
  </div>
  <div class="hero-badge">
    <span class="live-dot"></span>
    Neural infrastructure \u00b7 Live telemetry
  </div>
  <h1>Private Inference <span class="accent">for Ashat</span></h1>
</div>

<div class="container">
  <div id="status">{status_html}</div>
  <div id="brainstem">{brainstem_html}</div>
</div>

{footer_html}

<script>
(function() {{
    var REFRESH_MS = {safe_refresh * 1000};
    var STATUS_EL = document.getElementById('status');
    var BRAINSTEM_EL = document.getElementById('brainstem');
    function tick() {{
        fetch('/api/dashboard_html', {{ cache: 'no-store' }})
            .then(function(r) {{
                if (!r.ok) throw new Error('status ' + r.status);
                return r.json();
            }})
            .then(function(j) {{
                if (j.status_html && STATUS_EL) {{
                    STATUS_EL.innerHTML = j.status_html;
                }}
                if (j.brainstem_html && BRAINSTEM_EL) {{
                    BRAINSTEM_EL.innerHTML = j.brainstem_html;
                }}
            }})
            .catch(function(err) {{
                /* Silent: dashboard stays at last-good snapshot. */
                console.warn('dashboard refresh failed', err);
            }});

    }}

    setInterval(tick, REFRESH_MS);
    tick();
}})();
</script>
</body>
</html>"""
