"""Authentication helpers for the private BrainStem API."""

from __future__ import annotations

import hmac


def is_valid_api_key(supplied: str, expected: str) -> bool:
    """Return whether both configured and supplied keys match securely."""
    if not expected:
        return False
    return hmac.compare_digest(supplied, expected)
