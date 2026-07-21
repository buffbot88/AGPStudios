"""Response envelope → HTTP response adapter.

Pure-function adapter that converts a Run ``envelope`` dict into a
HTTP ``(status, body)`` tuple. Used by both FastAPI and Gradio handlers
(Gradio wraps the body in ``json.dumps`` and discards the status — it
returns the body as a string and uses a 200-equivalent for everything).

The adapter is intentionally tiny and dependency-free so it can be
imported into unit tests without dragging in gradio or fastapi.
"""

from __future__ import annotations

from typing import Any

from run_errors import ERROR_CODE_TO_HTTP_STATUS


def envelope_to_response(envelope: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Convert a Run envelope into an ``(HTTP_status, body)`` tuple.

    Success: ``ok=True`` envelopes return ``200`` and the body has the
    internal ``ok`` flag stripped (FastAPI/OpenAI-compatible shape).

    Failure: the HTTP status is looked up from
    :data:`run_errors.ERROR_CODE_TO_HTTP_STATUS`. Unknown codes fall back
    to ``500``. ``INVALID_REQUEST`` codes are forced to ``400``.
    ``UNAUTHORIZED`` codes are forced to ``401``.
    """
    if envelope.get("ok"):
        return 200, {k: v for k, v in envelope.items() if k != "ok"}

    err = envelope.get("error", {}) or {}
    code = err.get("code", "internal_error")
    status = ERROR_CODE_TO_HTTP_STATUS.get(code, 500)
    if code == "INVALID_REQUEST" or code.startswith("INVALID"):
        status = 400
    if code == "UNAUTHORIZED":
        status = 401
    return status, {"error": {"message": err.get("message", ""), "type": code.lower()}}
