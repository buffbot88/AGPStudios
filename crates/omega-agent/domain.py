"""Domain types for the AshatOS Neural Host — single-lane BrainStem.

This module owns the canonical lane name and configuration, plus request
validation that enforces lane constraints. It deliberately has zero heavy
runtime dependencies so it can be imported from any other module, including
unit tests.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

import os


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


# Configurable alias maps (overridable per-deployment). A request may identify
# the lane by:
#   - the canonical lane name (e.g. "brainstem")
#   - an AshatOS-style prefixed name (e.g. "ashat-brainstem")
#   - the configured GGUF filename for the lane (LANE_CONFIG[Lane.BRAINSTEM]["file"])
#
# Populated AFTER ``LANE_CONFIG`` is built so the GGUF filename aliases
# always match what ``lane_cfg(lane)["file"]`` returns, regardless of
# whether env overrides are present at import time.
BRAINSTEM_ALIASES: set[str] = set()


# Per-lane configuration. Kept here (not on the Lane enum) because the enum
# must remain stdlib-pure. Read on each boot from env vars; defaults match
# the new BrainStem model.
def _build_lane_config() -> dict[Lane, dict[str, Any]]:
    return {
        Lane.BRAINSTEM: {
            "label": "BrainStem",
            # Storage bucket path: ``buckets/{owner}/{bucket}``
            # Download URL: https://huggingface.co/buckets/{owner}/{bucket}/resolve/{file}
            "repo": os.getenv(
                "BRAINSTEM_MODEL_REPO",
                "buckets/stressthismess/ashatos-storage",
            ),
            "file": os.getenv(
                "BRAINSTEM_MODEL_FILE",
                "LFM2.5-1.2B-Instruct-Q8_0.gguf",
            ),
            "ctx": int(os.getenv("BRAINSTEM_CTX", "8192")),
            "max_tokens": int(os.getenv("BRAINSTEM_MAX_TOKENS", "8192")),
            "gpu_duration": int(os.getenv("BRAINSTEM_GPU_DURATION", "120")),
            "max_messages": 64,
            "max_body_bytes": 1_048_576,
            "model_path": "",
        },
    }


# Built once on import. env-var overrides must be set BEFORE app import
# (i.e. from the Hugging Face Space's Settings → Secrets).
LANE_CONFIG: dict[Lane, dict[str, Any]] = _build_lane_config()

# Populate alias set now that LANE_CONFIG exists, so env-overridden
# filenames are picked up.
BRAINSTEM_ALIASES.update({
    "brainstem",
    "ashat-brainstem",
    "LFM2.5 1.2B Instruct",
    "LFM2.5-1.2B",
    LANE_CONFIG[Lane.BRAINSTEM]["file"],
})
BRAINSTEM_ALIASES.discard("")


def lane_cfg(lane: Lane) -> dict[str, Any]:
    """Per-lane config dict (label, repo, file, ctx, ...)."""
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
    if len(messages) > cfg["max_messages"]:
        return f"Too many messages (max {cfg['max_messages']})"
    body_bytes = len(json.dumps(body))
    if body_bytes > cfg["max_body_bytes"]:
        return f"Request body too large (max {cfg['max_body_bytes']} bytes)"
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
