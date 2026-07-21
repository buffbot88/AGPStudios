"""Tests for the response envelope adapter (single BrainStem lane).

The Run pipeline emits one canonical envelope dict; the response adapter
(:func:`response_adapter.envelope_to_response`) converts it to the
OpenAI-compatible HTTP shape. These tests pin the HTTP status mapping so
FastAPI and Gradio surfaces are always in sync, and the envelope's
``ok=True`` flag is stripped before going onto the wire.

This test imports only :mod:`response_adapter` and :mod:`run_errors` \u2014 no
gradio, no fastapi, no NetworkX-shaped module side effects.
"""

from __future__ import annotations

import unittest

from response_adapter import envelope_to_response


class TestEnvelopeToResponse(unittest.TestCase):

    def test_success_strips_ok_and_returns_200(self) -> None:
        success_envelope = {
            "id": "ashat-abc",
            "object": "chat.completion",
            "created": 1,
            "model": "LFM2.5-1.2B-Instruct-Q8_0.gguf",
            "lane": "brainstem",
            "choices": [
                {"index": 0,
                 "message": {"role": "assistant", "content": "hi"},
                 "finish_reason": "stop"},
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "performance": {"cold_start": True, "total_latency_ms": 50.0},
            "request_id": "abc",
            "ok": True,
        }
        status, body = envelope_to_response(success_envelope)
        self.assertEqual(status, 200)
        self.assertNotIn("ok", body)
        self.assertEqual(body["id"], "ashat-abc")
        self.assertEqual(body["choices"][0]["message"]["content"], "hi")

    def test_inference_unavailable_returns_503(self) -> None:
        env = {
            "ok": False,
            "request_id": "x",
            "lane": "brainstem",
            "error": {
                "code": "INFERENCE_UNAVAILABLE",
                "message": "binary not installed",
                "retryable": False,
            },
        }
        status, body = envelope_to_response(env)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["type"], "inference_unavailable")

    def test_invalid_request_returns_400(self) -> None:
        env = {
            "ok": False,
            "request_id": "x",
            "lane": "brainstem",
            "error": {"code": "INVALID_REQUEST", "message": "bad", "retryable": False},
        }
        status, _ = envelope_to_response(env)
        self.assertEqual(status, 400)

    def test_unauthorized_returns_401(self) -> None:
        env = {
            "ok": False,
            "request_id": "x",
            "lane": "brainstem",
            "error": {"code": "UNAUTHORIZED", "message": "x", "retryable": False},
        }
        status, _ = envelope_to_response(env)
        self.assertEqual(status, 401)

    def test_server_start_failed_returns_503(self) -> None:
        env = {
            "ok": False,
            "request_id": "x",
            "lane": "brainstem",
            "error": {"code": "SERVER_START_FAILED", "message": "x", "retryable": True},
        }
        status, _ = envelope_to_response(env)
        self.assertEqual(status, 503)

    def test_inference_timeout_returns_503(self) -> None:
        env = {
            "ok": False,
            "request_id": "x",
            "lane": "brainstem",
            "error": {"code": "INFERENCE_TIMEOUT", "message": "x", "retryable": True},
        }
        status, _ = envelope_to_response(env)
        self.assertEqual(status, 503)

    def test_unmapped_code_falls_back_to_500(self) -> None:
        env = {
            "ok": False,
            "request_id": "x",
            "lane": "brainstem",
            "error": {"code": "SOMETHING_NEW", "message": "x", "retryable": True},
        }
        status, _ = envelope_to_response(env)
        self.assertEqual(status, 500)

    def test_error_body_shape_is_openai_compatible(self) -> None:
        env = {
            "ok": False,
            "request_id": "x",
            "lane": "brainstem",
            "error": {"code": "INFERENCE_FAILED", "message": "boom", "retryable": True},
        }
        _, body = envelope_to_response(env)
        self.assertEqual(body["error"]["message"], "boom")
        self.assertEqual(body["error"]["type"], "inference_failed")


if __name__ == "__main__":
    unittest.main()
