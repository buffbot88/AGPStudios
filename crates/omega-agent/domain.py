"""Domain types for the AshatOS dual-lane inference host.

This module owns the canonical names of the lanes and the per-lane
configuration table. It deliberately has zero runtime dependencies so it can
be imported from any other module, including the unit tests.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

import os


class Lane(str, Enum):
    """The two inference lanes — a closed enum, never a free string."""

    MICROBRAIN = "microbrain"
    MAINBRAIN = "mainbrain"

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
# a lane by:
#   - the canonical lane name (e.g. "mainbrain")
#   - an AshatOS-style prefixed name (e.g. "ashat-mainbrain")
#   - the configured GGUF filename for the lane (LANE_CONFIG[lane]["file"])
#
# Populated AFTER ``LANE_CONFIG`` is built so the GGUF filename aliases
# always match what ``lane_cfg(lane)["file"]`` returns, regardless of
# whether env overrides are present at import time.
MICROBRAIN_ALIASES: set[str] = set()
MAINBRAIN_ALIASES: set[str] = set()


# Per-lane configuration. Kept here (not on the Lane enum) because the enum
# must remain stdlib-pure. Read on each boot from env vars; defaults match
# the values previously hard-coded in app.py.
def _build_lane_config() -> dict[Lane, dict[str, Any]]:
    return {
        Lane.MICROBRAIN: {
            "label": "MicroBrain",
            "repo": os.getenv("MICRO_MODEL_REPO", "RipBuffy/LFM2.5-Q6_K"),
            "file": os.getenv("MICRO_MODEL_FILE", "LFM2.5-350M-Q6_K.gguf"),
            "ctx": int(os.getenv("MICRO_CTX", "1024")),
            "max_tokens": int(os.getenv("MICRO_MAX_TOKENS", "128")),
            "gpu_duration": int(os.getenv("MICRO_GPU_DURATION", "60")),
            "max_messages": 32,
            "max_body_bytes": 131_072,
            "model_path": "",
        },
        Lane.MAINBRAIN: {
            "label": "MainBrain",
            "repo": os.getenv("MAIN_MODEL_REPO", "RipBuffy/LFM2.5-Q6_K"),
            "file": os.getenv("MAIN_MODEL_FILE", "LFM2.5-1.2B-Instruct-Q6_K.gguf"),
            "ctx": int(os.getenv("MAIN_CTX", "1536")),
            "max_tokens": int(os.getenv("MAIN_MAX_TOKENS", "256")),
            "gpu_duration": int(os.getenv("MAIN_GPU_DURATION", "120")),
            "max_messages": 64,
            "max_body_bytes": 262_144,
            "model_path": "",
        },
    }


# Built once on import. env-var overrides must be set BEFORE app import
# (i.e. from the Hugging Face Space's Settings → Secrets).
LANE_CONFIG: dict[Lane, dict[str, Any]] = _build_lane_config()

# Populate alias sets now that LANE_CONFIG exists, so env-overridden
# filenames are picked up.
MICROBRAIN_ALIASES.update({
    "microbrain",
    "ashat-microbrain",
    LANE_CONFIG[Lane.MICROBRAIN]["file"],
})
MAINBRAIN_ALIASES.update({
    "mainbrain",
    "ashat-mainbrain",
    LANE_CONFIG[Lane.MAINBRAIN]["file"],
})
MICROBRAIN_ALIASES.discard("")
MAINBRAIN_ALIASES.discard("")


def lane_cfg(lane: Lane) -> dict[str, Any]:
    """Per-lane config dict (label, repo, file, ctx, ...)."""
    return LANE_CONFIG[lane]
