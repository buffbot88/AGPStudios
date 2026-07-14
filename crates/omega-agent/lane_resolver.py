"""Strict lane resolver — replaces substring-sniff routing.

Single source of truth for "given a request, which lane does this go to?"
Both adapters (Gradio route + HTTP ``model`` field) call into this function.
With only one lane (BrainStem), every valid request resolves to it.
Unknown names raise :class:`InvalidRequestError`.
"""

from __future__ import annotations

from domain import Lane, BRAINSTEM_ALIASES
from run_errors import InvalidRequestError


class LaneResolver:
    """Pure, deterministic lane routing from a request shape.

    Resolution rules:
      * If ``route_hint`` is provided and is the canonical lane name,
        that lane wins (the route's identity is authoritative).
      * Otherwise, look at ``payload['model']`` and match against the
        configurable alias map.
      * Anything that doesn't match exactly is :class:`InvalidRequestError` —
        never a silent fall-through.
    """

    def resolve(self, payload: dict, route_hint: str | None) -> Lane:
        # Authority of an authenticated route override.
        if route_hint is not None:
            try:
                return Lane.parse(route_hint)
            except ValueError:
                # Caller passed a bad route hint; this is a programmer
                # error, not a client error. Raise InvalidRequestError.
                raise InvalidRequestError(
                    f"internal route hint {route_hint!r} is not a valid Lane"
                )

        model = (payload.get("model") or "").strip()
        if not model:
            raise InvalidRequestError(
                "request body must include a 'model' field naming a known lane"
            )

        model_lower = model.lower()
        # Compare case-sensitively because gguf filenames ARE case-sensitive.
        if model in BRAINSTEM_ALIASES:
            return Lane.BRAINSTEM

        # Case-insensitive fallback for human-friendly aliases like
        # "BrainStem" or "brainstem".
        if model_lower in {a.lower() for a in BRAINSTEM_ALIASES}:
            return Lane.BRAINSTEM

        raise InvalidRequestError(
            f"unknown model {model!r}; expected one of: "
            f"{sorted(BRAINSTEM_ALIASES)}"
        )
