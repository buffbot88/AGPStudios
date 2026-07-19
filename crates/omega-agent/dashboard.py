"""AshatOS Neural Host — server-rendered public-telemetry dashboard.

Pivoted from Gradio-coupled dashboard (commit 153acd9 and prior) to a
pure FastAPI HTML rendering path. The build_dashboard() function +
DashboardTemplate dataclass from the prior version were Gradio-coupled
(gr.HTML, gr.Timer, gr.Row, gr.Column) and are dropped.

``render_index_html(snapshot_provider, refresh_seconds)`` returns a
self-contained ``<!DOCTYPE html>`` document that:

  * Shows the operator-facing header, status row, single BrainStem
    neural-lane card, and footer (server-rendered on first paint so
    the page is meaningful before the first poll lands).
  * Embeds a tiny JavaScript ``setInterval`` that polls
    ``GET /api/dashboard_html`` every ``refresh_seconds`` and replaces
    the status + brainstem-card ``innerHTML`` in place. This mirrors
    the previous Gradio ``gr.Timer`` behaviour with plain browser
    fetch; no Gradio runtime, no Auth shim.

``render_dashboard_html_json(snapshot)`` is the companion endpoint
payload -- returns the ``status_html`` + ``brainstem_html`` strings
that the JS poll swaps in.

The CSS, color palette, status pill, sparkline (inline SVG), and
BrainStem card markup are preserved unchanged from the pre-pivot
version so the public telemetry surface looks identical except for
the auto-refresh mechanism.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from public_snapshot import (
    DIAGNOSTIC_PILL_OVERRIDES,
    PUBLIC_ERROR_MESSAGES,
    PublicSnapshot,
)


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

_ACCENT = "#8B5CF6"
_GLOW = "rgba(139,92,246,0.18)"
_BRIGHT = "#A78BFA"


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
# Format helpers — unchanged from the pre-pivot version
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
    """Build the coloured status pill for a card."""
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
# Card builder — single BrainStem lane
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

    model_tooltip = model or ""

    diagnostic_html = ""
    if last_failure_code and reason_message:
        diag_color, _ = override_pill if override_pill else (_CORAL, "")
        diagnostic_html = (
            f'<div style="margin: 0 0 16px; padding: 12px 14px; '
            f'border: 1px solid {diag_color}66; border-radius: 10px; '
            f'background: {diag_color}14; color: {diag_color}; '
            f'font-size: 0.78em; line-height: 1.4; '
            f'font-family: sans-serif;">'
            f'<div style="font-weight: 700; letter-spacing: 0.04em; '
            f'margin-bottom: 4px; font-size: 0.82em;">'
            f'\u26a0  {last_failure_code.replace("_", " ").title()}'
            f'</div>'
            f'<div style="color: {_PRIMARY}; opacity: 0.92;">'
            f'{reason_message}'
            f'</div>'
            f'</div>'
        )

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
  <div style="position: absolute; top: -40px; left: 50%; transform: translateX(-50%);
       width: 180px; height: 80px; border-radius: 50%;
       background: {glow}; filter: blur(24px); pointer-events: none;"></div>

  <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px;">
    <div>
      <div style="font-size: 1.05em; font-weight: 700; color: {_PRIMARY};
           letter-spacing: 0.03em; font-family: sans-serif;">
        {lane_key.upper()}</div>
      <div style="font-size: 0.78em; color: {_SECONDARY}; margin-top: 2px;
           font-family: sans-serif;">
        Primary Inference Lane</div>
    </div>
    {_status_pill_html(state, override=override_pill)}
  </div>

  {diagnostic_html}

  <div style="margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid {_BORDER};">
    <div style="font-size: 0.85em; font-weight: 600; color: {bright};
         font-family: monospace;" title="{model_tooltip}">
      {short_model}</div>
    <div style="font-size: 0.75em; color: {_MUTED}; margin-top: 3px;
         font-family: monospace;">
      Context {ctx_fmt} \u00b7 <span title="{model_tooltip}" style="cursor: help; border-bottom: 1px dotted {_MUTED};">{model}</span>
    </div>
  </div>

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

  <div style="margin-bottom: 10px;">
    <div style="font-size: 0.6em; color: {_MUTED}; letter-spacing: 0.06em;
         font-weight: 600; font-family: sans-serif; text-transform: uppercase;
         margin-bottom: 2px;">
      Recent Generation Speed</div>
    {sparkline}
  </div>

  <div style="font-size: 0.7em; padding-top: 8px; border-top: 1px solid {_BORDER};
       display: flex; justify-content: space-between; align-items: center;">
    {footer}
  </div>
</div>"""


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
    """Static brand header with glowing brain badge."""
    return f"""\
<style>
  @media (prefers-reduced-motion: no-preference) {{
    @keyframes brain-pulse {{
      0%, 100% {{ box-shadow: 0 0 12px rgba(139,92,246,0.15), 0 0 28px rgba(139,92,246,0.06); }}
      50% {{ box-shadow: 0 0 18px rgba(139,92,246,0.30), 0 0 40px rgba(139,92,246,0.12); }}
    }}
    .brain-badge {{ animation: brain-pulse 3s ease-in-out infinite; }}
  }}
  .brain-badge {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 56px; height: 56px; border-radius: 16px;
    background: linear-gradient(135deg, rgba(139,92,246,0.12) 0%, rgba(244,114,182,0.08) 100%);
    border: 1px solid rgba(139,92,246,0.20);
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
    BrainStem Neural Inference \u00b7 Public Telemetry</p>
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

    last_failure_codes = [
        l.get("last_failure_code") for l in lanes.values()
        if l.get("last_failure_code")
    ]
    priority_order = (
        "HF_CREDITS_EXHAUSTED",
        "HF_RATE_LIMITED",
        "MODEL_DOWNLOAD_FAILED",
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

    headline_html = ""
    if headline_code and headline_msg:
        headline_html = (
            f'<div style="margin: 6px 20px 0; text-align: center; '
            f'font-family: sans-serif; font-size: 0.78em; color: {_CORAL};">'
            f'\u26a0 <span style="font-weight: 600;">{headline_code.replace("_", " ").title()}</span>'
            f' \u00b7 <span>{headline_msg}</span>'
            f'</div>'
        )

    return f"""\
<div style="text-align: center; padding: 6px 20px 12px;">
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
    """Static footer bar."""
    return f"""\
<div style="text-align: center; padding: 16px 20px 24px;">
  <span style="font-size: 0.68em; color: {_MUTED}; font-family: sans-serif;
       letter-spacing: 0.03em;">
    BrainStem inference engine \u00b7 Public telemetry only</span>
</div>"""


    # Live-refresh polling is wired INSIDE app.py via `gr.HTML`'s
    # `js_on_load` parameter instead. See :func:`render_dashboard_refresh_js`.


# ──────────────────────────────────────────────────────────────────────────
# Public rendering entry points — server-rendered HTML fragment,
# companion JSON payload used by the JS poll loop, and the polling
# function expression exported for gr.HTML's `js_on_load`.
# ──────────────────────────────────────────────────────────────────────────


def render_dashboard_refresh_js(refresh_ms: int) -> str:
    """Return a JS function expression for `gr.HTML(js_on_load=...)`.

    Browsers do NOT execute `<script>` tags injected via innerHTML
    (which is how `gr.HTML(value=...)` renders its content). The polling
    loop therefore has to be supplied through Gradio's `js_on_load`
    parameter — a function expression Gradio evaluates on component
    render. Inside Gradio this runs ONCE on mount; we use ``setInterval``
    to keep ticking every ``refresh_ms`` and one immediate ``tick()``
    call to correct any drift between server-render time and the
    current second.
    """
    safe_ms = max(1000, int(refresh_ms))
    return (
        "() => {\n"
        "    const REFRESH_MS = %d;\n"
        "    function tick() {\n"
        "        fetch('/api/dashboard_html', { cache: 'no-store' })\n"
        "            .then(function (r) {\n"
        "                if (!r.ok) throw new Error('status ' + r.status);\n"
        "                return r.json();\n"
        "            })\n"
        "            .then(function (j) {\n"
        "                if (j.status_html) {\n"
        "                    var el = document.getElementById('status');\n"
        "                    if (el) el.innerHTML = j.status_html;\n"
        "                }\n"
        "                if (j.brainstem_html) {\n"
        "                    var el = document.getElementById('brainstem');\n"
        "                    if (el) el.innerHTML = j.brainstem_html;\n"
        "                }\n"
        "            })\n"
        "            .catch(function (err) {\n"
        "                console.warn('dashboard refresh failed', err);\n"
        "            });\n"
        "    }\n"
        "    setInterval(tick, REFRESH_MS);\n"
        "    tick();\n"
        "}\n"
    ) % safe_ms


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


# JavaScript snippet that the rendered HTML page embeds. Pulled out as
# a constant so it can be tested independently if needed.
_REFRESH_JS_TEMPLATE = """\
(function() {
    var REFRESH_MS = %REFRESH_MS%;
    var STATUS_EL = document.getElementById('status');
    var BRAINSTEM_EL = document.getElementById('brainstem');
    function tick() {
        fetch('/api/dashboard_html', { cache: 'no-store' })
            .then(function(r) {
                if (!r.ok) throw new Error('status ' + r.status);
                return r.json();
            })
            .then(function(j) {
                if (j.status_html && STATUS_EL) {
                    STATUS_EL.innerHTML = j.status_html;
                }
                if (j.brainstem_html && BRAINSTEM_EL) {
                    BRAINSTEM_EL.innerHTML = j.brainstem_html;
                }
            })
            .catch(function(err) {
                /* Silent: dashboard stays at last-good snapshot so a
                   transient /api/dashboard_html failure doesn't blank
                   the operator's view. */
                console.warn('dashboard refresh failed', err);
            });
    }
    setInterval(tick, REFRESH_MS);
    /* Trigger one immediate tick so any divergence between server-render
       time and the current second is corrected on first browser paint. */
    tick();
})();
"""


def render_index_html(
    snapshot_provider: Callable[[], PublicSnapshot],
    refresh_seconds: int = 8,
) -> str:
    """Render the public dashboard as a compact inner-only HTML fragment.

    Returns a single ``<div class="ashat-root">...</div>`` wrapper
    containing a scoped ``<style>`` block (every selector prefixed
    with ``.ashat-root``) plus the page body (header + status row +
    BrainStem card + footer) and a ``<script>`` that polls
    ``/api/dashboard_html`` every ``refresh_seconds`` for live updates.

    Why compact inner-only instead of a standalone ``<!DOCTYPE html>``
    document: the value flows through ``gr.HTML(value=...)`` and into
    a Gradio Blocks UI; browsers silently strip DOCTYPE/html/head/body
    tags injected via ``innerHTML``, so an unscoped full-document body
    would lose its background / typography class hooks. Scoping all
    global CSS rules under ``.ashat-root`` and wrapping everything in
    a single styled div makes the dashboard render correctly inside
    Gradio's chrome-less container — and the inline styles already
    painted on each card stay identical to the standalone version.

    ``refresh_seconds`` is clamped to ``>= 1`` so a misconfigured
    zero/negative value can't loop the JS poll at full CPU.
    """
    safe_refresh = max(1, int(refresh_seconds))
    initial_snapshot = snapshot_provider()

    header_html = _build_header_html()
    status_html = _build_status_row_html(initial_snapshot)
    brainstem_html = _build_cards_html(initial_snapshot)
    footer_html = _build_footer_html()

    return f"""<div class="ashat-root">
  <style>
    .ashat-root {{
      background: {_BG}; color: {_PRIMARY};
      margin: 0; padding: 0; min-height: 100vh;
      font-family: sans-serif;
    }}
    .ashat-root a {{ color: {_ACCENT}; }}
    .ashat-root .container {{
      max-width: 760px; margin: 0 auto; padding: 0 24px;
    }}
    .ashat-root #status, .ashat-root #brainstem {{
      line-height: 1.4;
    }}
  </style>
  {header_html}
  <div id="status">{status_html}</div>
  <div class="container">
    <div id="brainstem">{brainstem_html}</div>
  </div>
  {footer_html}
</div>"""
    # Live-refresh polling is wired INSIDE app.py via gr.HTML's `js_on_load`
    # parameter, not by embedding a <script> tag here. Browsers do NOT
    # execute <script> tags injected via innerHTML (which is how Gradio's
    # gr.HTML(value=...) renders its content into the DOM); only Gradio's
    # `js_on_load=` runs on render. The polling snippet is exported as
    # `render_dashboard_refresh_js(refresh_ms)` for the caller to wire in.
