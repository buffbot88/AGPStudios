"""Domain types for the AshatOS Neural Host — single-lane BrainStem.

This module owns the canonical lane name and configuration plus request
validation. It deliberately has no heavy runtime dependencies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from config import CONFIG



class Lane(str, Enum):
    """The single inference lane — a closed enum, never a free string."""

    BRAINSTEM = "brainstem"

    @classmethod
    def parse(cls, value: str) -> "Lane":
        """Strict coercion: an unknown lane string raises ValueError."""
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(
                f"unknown lane {value!r}; expected one of: "
                f"{', '.join(repr(m.value) for m in cls)}"
            ) from exc


@dataclass
class LaneConfig:
    """Typed per-lane configuration.

    All fields have defaults matching the BrainStem model. Values are loaded
    from the centralized JSON server configuration.
    """
    label: str = "BrainStem"
    file: str = "LFM2.5-1.2B-Instruct-Q8_0.gguf"
    ctx: int = 4096
    max_tokens: int = 1024
    max_messages: int = 64
    max_body_bytes: int = 1_048_576
    model_path: str = ""


# Configurable alias maps (overridable per-deployment). A request may identify
# the lane by:
#   - the canonical lane name (e.g. "brainstem")
#   - an AshatOS-style prefixed name (e.g. "ashat-brainstem")
#   - the configured GGUF filename for the lane (``LANE_CONFIG[Lane.BRAINSTEM].file``)
#
# Populated after ``LANE_CONFIG`` is built so the configured GGUF filename
# is available as a request alias.
BRAINSTEM_ALIASES: set[str] = set()


# Per-lane configuration. Kept here (not on the Lane enum) because the enum
# must remain stdlib-pure. Values come from the local JSON configuration.
def _build_lane_config() -> dict[Lane, LaneConfig]:
    return {
        Lane.BRAINSTEM: LaneConfig(
            label="BrainStem",
            file=CONFIG.model_file,
            ctx=CONFIG.context,
            max_tokens=CONFIG.max_tokens,
            max_messages=64,
            max_body_bytes=1_048_576,
            model_path=CONFIG.model_path,
        ),
    }


# Built once on import from the JSON configuration.
LANE_CONFIG: dict[Lane, LaneConfig] = _build_lane_config()

# Populate alias set now that LANE_CONFIG exists, so configured filenames
# are picked up.
BRAINSTEM_ALIASES.update({
    "brainstem",
    "ashat-brainstem",
    "LFM2.5 1.2B Instruct",
    "LFM2.5-1.2B",
    LANE_CONFIG[Lane.BRAINSTEM].file,
})
BRAINSTEM_ALIASES.discard("")


def lane_cfg(lane: Lane) -> LaneConfig:
    """Per-lane BrainStem config (file, context, and limits)."""
    return LANE_CONFIG[lane]


# ──────────────────────────────────────────────────────────────────────────
# Request validation — kept here so constraints live near their data.
# ──────────────────────────────────────────────────────────────────────────


def validate_request(body: dict[str, Any], lane: Lane) -> str | None:
    """Validate a request body against lane constraints.

    Returns ``None`` if valid, or an error message string if invalid.
    """
    cfg = lane_cfg(lane)
    messages = body.get("messages", [])
    if not messages or not isinstance(messages, list):
        return "Missing or invalid 'messages' field"
    if len(messages) > cfg.max_messages:
        return f"Too many messages (max {cfg.max_messages})"
    body_bytes = len(json.dumps(body))
    if body_bytes > cfg.max_body_bytes:
        return f"Request body too large (max {cfg.max_body_bytes} bytes)"
    for msg in messages:
        if not isinstance(msg, dict):
            return "Each message must be a dict"
        role = msg.get("role", "")
        if role not in ("system", "user", "assistant"):
            return f"Unsupported role: {role}"
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.strip():
            return "Message content must be a non-empty string"
    max_tokens = body.get("max_tokens", 0)
    if max_tokens and (not isinstance(max_tokens, (int, float)) or max_tokens < 1):
        return "max_tokens must be a positive integer"
    temperature = body.get("temperature", 0.7)
    if isinstance(temperature, (int, float)) and (temperature < 0 or temperature > 2):
        return "temperature must be between 0 and 2"
    top_p = body.get("top_p", 0.9)
    if isinstance(top_p, (int, float)) and (top_p < 0 or top_p > 1):
        return "top_p must be between 0 and 1"
    if body.get("stream", False):
        return "Streaming is not yet supported"
    return None
