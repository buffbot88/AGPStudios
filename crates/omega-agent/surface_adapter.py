"""SurfaceAdapter seam — shared inference pipeline for all transports.

Previously the Gradio and FastAPI handlers in ``app.py`` duplicated the
same 4-step pipeline (auth → parse → validate → execute → respond) as
inline closures with slightly different error handling and response
shaping.

Now:
  1. A single ``run_surface()`` function owns the shared pipeline.
  2. Each transport provides thin header-extraction and response-marshalling
     via a lightweight :class:`SurfaceAdapter` protocol.
  3. ``SurfaceAdapter`` implementations are 5–10 lines each.

The seam is the **test surface**: a ``FakeAdapter`` can exercise every
error path in one test class without booting Gradio or FastAPI.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from domain import Lane, validate_request
from lane_keygate import AuthError, LaneKeyGate
from lane_resolver import LaneResolver
from run_errors import InvalidRequestError


# ──────────────────────────────────────────────────────────────────────────
# SurfaceAdapter — transport-agnostic seam
# ──────────────────────────────────────────────────────────────────────────


class SurfaceAdapter:
    """Transport-specific seam. Implement three methods per transport.

    Methods
    -------
    extract_headers(request)
        Pull a plain ``dict[str, str]`` of HTTP headers from the request.
    respond_ok(envelope: dict) -> Any
        Convert a success pipeline envelope into a transport-native response.
    respond_error(status: int, payload: dict) -> Any
        Convert an error payload into a transport-native response.
    """

    def extract_headers(self, request) -> dict[str, str]:
        raise NotImplementedError  # pragma: no cover

    def respond_ok(self, envelope: dict) -> Any:
        raise NotImplementedError  # pragma: no cover

    def respond_error(self, status: int, payload: dict) -> Any:
        raise NotImplementedError  # pragma: no cover


# ──────────────────────────────────────────────────────────────────────────
# Concrete adapters
# ──────────────────────────────────────────────────────────────────────────


class GradioSurfaceAdapter(SurfaceAdapter):
    """Adapter for Gradio queue-API handlers."""

    def extract_headers(self, request) -> dict[str, str]:
        from lane_keygate import headers_from_gradio
        return headers_from_gradio(request)

    def respond_ok(self, envelope: dict) -> str:
        return json.dumps(envelope)

    def respond_error(self, status: int, payload: dict) -> str:
        return json.dumps(payload)


class FastAPISurfaceAdapter(SurfaceAdapter):
    """Adapter for FastAPI request handlers."""

    def extract_headers(self, request) -> dict[str, str]:
        from lane_keygate import headers_from_fastapi
        return headers_from_fastapi(request)

    def respond_ok(self, envelope: dict) -> Any:
        from fastapi.responses import JSONResponse
        from response_adapter import envelope_to_response
        status, payload = envelope_to_response(envelope)
        return JSONResponse(status_code=status, content=payload)

    def respond_error(self, status: int, payload: dict) -> Any:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=status, content=payload)


# ──────────────────────────────────────────────────────────────────────────
# Shared pipeline
# ──────────────────────────────────────────────────────────────────────────


def run_surface(
    *,
    headers: dict[str, str],
    body: dict[str, Any] | None,
    body_parse_failed: bool,
    key_gate: LaneKeyGate,
    execute_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    lane: Lane | None = None,
    resolver: LaneResolver | None = None,
) -> dict[str, Any]:
    """Auth → parse → validate → execute pipeline, returning an envelope.

    Parameters
    ----------
    headers
        HTTP headers extracted from the transport request.
    body
        Parsed request body dict, or ``None`` if parsing failed or body was
        JSON ``null``. ``None`` is treated as ``INVALID_REQUEST`` regardless
        of ``body_parse_failed``.
    body_parse_failed
        ``True`` if the transport was unable to parse the request body
        (e.g. invalid JSON). When ``True``, the returned envelope carries
        an ``INVALID_REQUEST`` error regardless of other fields.
    key_gate
        The :class:`LaneKeyGate` instance used for auth.
    execute_fn
        A callable ``(lane_str, body) -> envelope dict`` — typically
        :func:`app.execute_lane`.
    lane
        Pre-resolved lane (for fixed-route handlers like Gradio).
    resolver
        Dynamic :class:`LaneResolver` (for the FastAPI OpenAPI-compatible
        endpoint). Ignored if ``lane`` is already provided.

    Returns
    -------
    dict
        A pipeline envelope dict. The caller marshals it to the transport
        via ``adapter.respond_ok()`` or ``adapter.respond_error()``.
    """
    if body_parse_failed or body is None:
        return {
            "ok": False,
            "error": {
                "code": "INVALID_REQUEST",
                "message": "Invalid or empty request body",
                "retryable": False,
            },
        }

    # ── Resolve lane ─────────────────────────────────────────────────
    resolved_lane = lane
    if resolved_lane is None and resolver is not None:
        try:
            resolved_lane = resolver.resolve(body, route_hint=None)
        except InvalidRequestError as exc:
            return {"ok": False, "error": exc.to_envelope()}

    if resolved_lane is None:
        return {
            "ok": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "lane could not be resolved",
                "retryable": False,
            },
        }

    # ── Auth ─────────────────────────────────────────────────────────
    try:
        key_gate.check(headers, resolved_lane)
    except AuthError:
        return {
            "ok": False,
            "error": {
                "code": "UNAUTHORIZED",
                "message": "unauthorized",
                "retryable": False,
            },
        }

    # ── Validate ─────────────────────────────────────────────────────
    if body is not None:
        try:
            err = validate_request(body, resolved_lane)
            if err:
                raise InvalidRequestError(err)
        except InvalidRequestError as exc:
            return {
                "ok": False,
                "request_id": str(uuid.uuid4()),
                "lane": resolved_lane.value,
                "error": exc.to_envelope(),
            }

    # ── Execute ──────────────────────────────────────────────────────
    return execute_fn(resolved_lane.value, body or {})
