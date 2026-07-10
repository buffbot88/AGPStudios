"""Strict lane resolver — replaces substring-sniff routing.

Single source of truth for "given a request, which lane does this go to?"
Both adapters (Gradio route + HTTP ``model`` field) call into this function.
Unknown names raise :class:`InvalidRequestError` — never silently fall through
to MainBrain.
"""

from __future__ import annotations

from domain import Lane, MICROBRAIN_ALIASES, MAINBRAIN_ALIASES
from run_errors import InvalidRequestError


class LaneResolver:
    """Pure, deterministic lane routing from a request shape.

    Resolution rules:
      * If ``route_hint`` is provided and is one of the canonical lane names,
        that lane wins (the route's identity is authoritative).
      * Otherwise, look at ``payload['model']`` and match against the
        configurable alias maps.
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
        if model in MICROBRAIN_ALIASES or model in MAINBRAIN_ALIASES:
            if model in MICROBRAIN_ALIASES and model in MAINBRAIN_ALIASES:
                # Alias collision (two lanes share a name). Prefer the more
                # specific one — both are kept but we disambiguate by length
                # here. In practice this should never fire.
                # Fall through: defaulted to MAINBRAIN only if no other match.
                # Treat as invalid to surface the duplication to operators.
                raise InvalidRequestError(
                    f"model name {model!r} maps to both lanes; "
                    f"fix MICROBRAIN_ALIASES / MAINBRAIN_ALIASES"
                )
            if model in MICROBRAIN_ALIASES:
                return Lane.MICROBRAIN
            return Lane.MAINBRAIN

        # Case-insensitive fallback for human-friendly aliases like
        # "MicroBrain" or "MainBrain" only.
        if model_lower in {a.lower() for a in MICROBRAIN_ALIASES}:
            return Lane.MICROBRAIN
        if model_lower in {a.lower() for a in MAINBRAIN_ALIASES}:
            return Lane.MAINBRAIN

        raise InvalidRequestError(
            f"unknown model {model!r}; expected one of: "
            f"{sorted(MICROBRAIN_ALIASES | MAINBRAIN_ALIASES)}"
        )
