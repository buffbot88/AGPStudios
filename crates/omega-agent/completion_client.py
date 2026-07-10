"""CompletionClient — byte-for-byte interaction with the llama-server HTTP API.

Knows nothing about subprocesses, Hugging Face, or metrics; talks HTTP only.
Translates server non-200 / malformed bodies into typed :mod:`run_errors`
exceptions; never bubbles a raw ``requests.RequestException``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from backend_launcher import LiveBackend
from domain import Lane, lane_cfg
from run_errors import (
    CompletionProtocolError,
    CompletionTimeout,
    InvalidModelResponse,
)


@dataclass
class CompletionResult:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    finish_reason: str | None = None
    prompt_tokens_per_second: float | None = None
    generation_tokens_per_second: float | None = None
    time_to_first_token_ms: float | None = None
    raw_response: dict = field(default_factory=dict)


class CompletionClient:
    """Stateless HTTP wrapper. Reusable across requests."""

    def __init__(self, default_timeout_s: float = 120.0) -> None:
        self.default_timeout_s = default_timeout_s

    def complete(
        self,
        backend: LiveBackend,
        lane: Lane,
        payload: dict[str, Any],
    ) -> CompletionResult:
        """Send one chat-completion request to the live backend."""
        # Lazy import — requests is heavy.
        import requests
        cfg = lane_cfg(lane)
        max_tokens = min(
            int(payload.get("max_tokens", cfg["max_tokens"])),
            cfg["max_tokens"],
        )

        body = {
            "model": cfg["file"],
            "messages": payload.get("messages", []),
            "max_tokens": max_tokens,
            "temperature": float(payload.get("temperature", 0.7)),
            "top_p": float(payload.get("top_p", 0.9)),
            "stream": False,
        }
        url = f"{backend.base_url}/chat/completions"

        t_inference_start = time.perf_counter()
        try:
            resp = requests.post(url, json=body, timeout=self.default_timeout_s)
        except requests.exceptions.Timeout as exc:
            raise CompletionTimeout(
                f"POST {url} timed out after {self.default_timeout_s}s"
            ) from exc
        except requests.RequestException as exc:
            raise CompletionProtocolError(
                f"POST {url} failed: {type(exc).__name__}: {exc}"
            ) from exc
        inference_ms = round((time.perf_counter() - t_inference_start) * 1000, 1)

        if resp.status_code != 200:
            raise CompletionProtocolError(
                f"completion returned HTTP {resp.status_code}: "
                f"{(resp.text or '')[:200]}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise InvalidModelResponse(
                f"completion body could not be parsed as JSON: {exc}"
            )

        try:
            choices = data["choices"]
            text = choices[0]["message"]["content"]
            finish_reason = choices[0].get("finish_reason", "stop")
        except (KeyError, IndexError, TypeError) as exc:
            raise InvalidModelResponse(
                f"completion body shape did not match OpenAI-compatible: {exc}"
            )

        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = (
            (prompt_tokens + completion_tokens)
            if prompt_tokens is not None and completion_tokens is not None
            else usage.get("total_tokens")
        )

        # Tokens/sec are derived assuming inference_ms is mostly the
        # generation phase (after boot). The orchestrator may refine.
        gen_ms = max(1.0, inference_ms)
        prompt_tps = (
            round(prompt_tokens / (gen_ms / 1000), 2)
            if prompt_tokens else None
        )
        gen_tps = (
            round(completion_tokens / (gen_ms / 1000), 2)
            if completion_tokens else None
        )

        return CompletionResult(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            finish_reason=finish_reason,
            prompt_tokens_per_second=prompt_tps,
            generation_tokens_per_second=gen_tps,
            raw_response=data,
        )
