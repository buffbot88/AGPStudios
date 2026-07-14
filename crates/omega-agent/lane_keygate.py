"""Single authentication authority — the LaneKeyGate.

Consolidates the previously-duplicated ``require_key`` (Gradio Request) and
``require_key_http`` (dict headers) into one function. Thin adapters above
this module extract a headers dict from whichever transport the request
arrived on; this module owns the actual comparison logic.

Design constraints:
    * Read the ``X-Ashat-Key`` header.
    * Select the expected secret for the lane (``ASHAT_BRAINSTEM_KEY`` env var).
    * Use :func:`hmac.compare_digest` (constant-time).
    * Raise a single generic :class:`AuthError` on rejection — never log
      the supplied key, never log the expected key.
    * If the host has no key configured for that lane, allow the request
      through (degraded-dev convenience; explicitly documented in
      SECURITY_NOTES.md).
"""

from __future__ import annotations

import hmac
import os
from typing import Mapping

from domain import Lane


class AuthError(Exception):
    """Raised when a request fails auth. Generic — no key material in str()."""

    def __init__(self, lane: Lane, status: int = 401) -> None:
        super().__init__(f"unauthorized for lane {lane.value}")
        self.lane = lane
        self.status = status


class LaneKeyGate:
    """Auth check, keyed by lane."""

    def __init__(self) -> None:
        # Read env once at construction so deployments that rotate keys
        # by re-importing app.py without restarting the process still
        # pick up the rotation.
        self._keys: dict[Lane, str] = {
            Lane.BRAINSTEM: os.getenv("ASHAT_BRAINSTEM_KEY", "") or "",
        }
        # Master key — overrides lane-specific checks. Set via
        # ASHAT_ADMIN_KEY env var. The AshatOS sync token
        # (AshatOS-00192) is the default for dev connections.
        self._master_key: str = os.getenv("ASHAT_ADMIN_KEY", "AshatOS-00192")

    def reload(self) -> None:
        """Re-read keys from env. Call after Space Secret rotation."""
        self._keys = {
            Lane.BRAINSTEM: os.getenv("ASHAT_BRAINSTEM_KEY", "") or "",
        }
        self._master_key = os.getenv("ASHAT_ADMIN_KEY", "AshatOS-00192")

    def expected_key(self, lane: Lane) -> str:
        return self._keys.get(lane, "")

    def check(self, headers: Mapping[str, str], lane: Lane) -> None:
        """Raise :class:`AuthError` if headers don't carry the right key.

        Adapter responsibility is to produce ``headers`` from whichever
        transport the request arrived on (Gradio or FastAPI). This method
        does the rest — and never logs key material.

        Accepts key from:
        * ``X-Ashat-Key`` header (AshatOS protocol)
        * ``Authorization: Bearer <key>`` header (OpenAI-compatible standard)
        * Lane-specific key (ASHAT_BRAINSTEM_KEY)
        * Master admin key (ASHAT_ADMIN_KEY, default AshatOS-00192)
        """
        expected = self._keys.get(lane, "")
        if not expected:
            return
        supplied = self._extract_key(headers)
        if not supplied:
            raise AuthError(lane)
        if hmac.compare_digest(supplied, expected):
            return
        if self._master_key and hmac.compare_digest(supplied, self._master_key):
            return
        raise AuthError(lane)

    @staticmethod
    def _extract_key(headers: Mapping[str, str]) -> str:
        """Extract the API key from request headers.

        Checks in order:
        1. X-Ashat-Key header
        2. Authorization: Bearer <token> header
        """
        for k, v in headers.items():
            kl = k.lower() if isinstance(k, str) else ""
            if kl == "x-ashat-key":
                return (v or "").strip()
        for k, v in headers.items():
            kl = k.lower() if isinstance(k, str) else ""
            if kl == "authorization":
                val = (v or "").strip()
                if val.lower().startswith("bearer "):
                    return val[7:].strip()
        return ""


# Adapter helpers — extract a dict-of-headers from a Gradio Request or a
# FastAPI Request.headers mapping. Kept here so the gate is a complete
# drop-in for the prior duplicates.

def headers_from_gradio(request) -> dict[str, str]:
    """Pull a headers dict out of a Gradio ``gr.Request`` object."""
    raw = getattr(request, "headers", None) or {}
    try:
        return {str(k): str(v) for k, v in dict(raw).items()}
    except (TypeError, ValueError):
        return {}


def headers_from_fastapi(request) -> dict[str, str]:
    """Pull a headers dict out of a FastAPI ``Request``."""
    raw = getattr(request, "headers", None) or {}
    try:
        return {str(k): str(v) for k, v in dict(raw).items()}
    except (TypeError, ValueError):
        return {}
