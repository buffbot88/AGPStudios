"""Tests for CompletionClient response parsing.

Pins the request-shape contract against llama-server's
``/v1/chat/completions``. The tests use ``unittest.mock`` to fake the
HTTP layer — no real llama-server, no GGUF, no ZeroGPU required.
"""

from __future__ import annotations

import unittest
from unittest import mock

from completion_client import CompletionClient
from domain import Lane
from run_errors import (
    CompletionProtocolError,
    CompletionTimeout,
    InvalidModelResponse,
)


def _make_live_backend(model_path: str = "/tmp/model.gguf") -> mock.Mock:
    backend = mock.Mock()
    backend.base_url = "http://127.0.0.1:18080/v1"
    backend.model_path = model_path
    backend.server_start_ms = 100.0
    backend.model_load_ms = 50.0
    backend.backend_mode = "cpu"
    backend.gpu_offload_verified = False
    backend.process = mock.Mock()
    return backend


class TestCompletionClient(unittest.TestCase):

    def setUp(self) -> None:
        self.client = CompletionClient(default_timeout_s=120.0)

    def test_successful_completion(self) -> None:
        ok_body = {
            "id": "chatcmpl-abc",
            "object": "chat.completion",
            "created": 1234567890,
            "model": "LFM2.5-350M-Q6_K.gguf",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            },
        }
        ok_resp = mock.Mock(status_code=200)
        ok_resp.json.return_value = ok_body

        with mock.patch("requests.post", return_value=ok_resp) as post:
            result = self.client.complete(
                _make_live_backend(), Lane.MICROBRAIN,
                {"messages": [{"role": "user", "content": "Hi"}]},
            )

        self.assertEqual(result.text, "Hello!")
        self.assertEqual(result.prompt_tokens, 5)
        self.assertEqual(result.completion_tokens, 3)
        self.assertEqual(result.total_tokens, 8)
        self.assertEqual(result.finish_reason, "stop")
        # POST URL must be the live backend's chat completions.
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertTrue(args[0].endswith("/v1/chat/completions"))
        self.assertIn("json", kwargs)
        # Body must include model name from the lane config.
        self.assertEqual(
            kwargs["json"]["model"],
            "LFM2.5-350M-Q6_K.gguf",
        )

    def test_non_200_raises_protocol_error(self) -> None:
        bad_resp = mock.Mock(status_code=503)
        bad_resp.text = "overloaded"
        with mock.patch("requests.post", return_value=bad_resp):
            with self.assertRaises(CompletionProtocolError):
                self.client.complete(
                    _make_live_backend(), Lane.MICROBRAIN,
                    {"messages": [{"role": "user", "content": "Hi"}]},
                )

    def test_timeout_maps_to_typed_error(self) -> None:
        import requests
        with mock.patch(
            "requests.post",
            side_effect=requests.exceptions.Timeout("timed out"),
        ):
            with self.assertRaises(CompletionTimeout):
                self.client.complete(
                    _make_live_backend(), Lane.MICROBRAIN,
                    {"messages": [{"role": "user", "content": "Hi"}]},
                )

    def test_connection_error_maps_to_protocol_error(self) -> None:
        import requests
        with mock.patch(
            "requests.post",
            side_effect=requests.exceptions.ConnectionError("nope"),
        ):
            with self.assertRaises(CompletionProtocolError):
                self.client.complete(
                    _make_live_backend(), Lane.MICROBRAIN,
                    {"messages": [{"role": "user", "content": "Hi"}]},
                )

    def test_malformed_body_raises_invalid_model_response(self) -> None:
        # 200 but the body isn't a JSON object.
        no_json = mock.Mock(status_code=200)
        no_json.json.side_effect = ValueError("not json")
        with mock.patch("requests.post", return_value=no_json):
            with self.assertRaises(InvalidModelResponse):
                self.client.complete(
                    _make_live_backend(), Lane.MICROBRAIN,
                    {"messages": [{"role": "user", "content": "Hi"}]},
                )

    def test_missing_choices_raises_invalid_model_response(self) -> None:
        ok_resp = mock.Mock(status_code=200)
        ok_resp.json.return_value = {"usage": {"prompt_tokens": 1}}
        with mock.patch("requests.post", return_value=ok_resp):
            with self.assertRaises(InvalidModelResponse):
                self.client.complete(
                    _make_live_backend(), Lane.MICROBRAIN,
                    {"messages": [{"role": "user", "content": "Hi"}]},
                )

    def test_max_tokens_is_capped(self) -> None:
        ok_body = {
            "choices": [
                {"message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        ok_resp = mock.Mock(status_code=200)
        ok_resp.json.return_value = ok_body
        with mock.patch("requests.post", return_value=ok_resp) as post:
            # Caller asks for 99999 — must be capped at lane's max_tokens (4096).
            self.client.complete(
                _make_live_backend(), Lane.MICROBRAIN,
                {"messages": [{"role": "user", "content": "Hi"}],
                 "max_tokens": 99999},
            )
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["json"]["max_tokens"], 4096)


if __name__ == "__main__":
    unittest.main()
