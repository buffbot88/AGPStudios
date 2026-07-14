"""Tests for RunMetrics redaction (single BrainStem lane).

The metrics store is exposed on the public dashboard; this test pins that
prompts and generated text never enter recorded metrics or events.
"""

from __future__ import annotations

import unittest
from unittest import mock

from backend_launcher import LiveBackend
from completion_client import CompletionResult
from domain import Lane
from metrics_store import MetricsStore
from run_errors import InferenceUnavailableError
from run_metrics import RunMetrics


def _fake_live_backend() -> LiveBackend:
    proc = mock.Mock()
    proc.poll.return_value = None
    return LiveBackend(
        lane=Lane.BRAINSTEM,
        process=proc,
        base_url="http://127.0.0.1:18080/v1",
        model_path="/tmp/model.gguf",
        server_start_ms=120.0,
        model_load_ms=80.0,
        backend_mode="cpu",
        gpu_offload_verified=False,
    )


def _fake_completion(text: str = "Hello!") -> CompletionResult:
    return CompletionResult(
        text=text,
        prompt_tokens=5,
        completion_tokens=3,
        total_tokens=8,
        finish_reason="stop",
        prompt_tokens_per_second=42.0,
        generation_tokens_per_second=18.0,
    )


class TestRunMetrics(unittest.TestCase):

    def test_record_success_stores_no_text(self) -> None:
        store = MetricsStore()
        m = RunMetrics(store)
        m.record_success(
            Lane.BRAINSTEM,
            _fake_live_backend(),
            _fake_completion("SUPER-SECRET-REPLY"),
            total_latency_ms=300.0,
            cold_start=False,
        )
        # The completion text must NOT appear anywhere in stored metrics.
        events = store.get_events()
        records = store.get_lane_metrics("brainstem")
        for s in events:
            self.assertNotIn("SUPER-SECRET-REPLY", s)
        for rec in records:
            self.assertNotIn("SUPER-SECRET-REPLY", str(rec.__dict__))
            self.assertNotIn("SUPER-SECRET-REPLY", str(rec))

    def test_record_success_records_token_counts(self) -> None:
        store = MetricsStore()
        m = RunMetrics(store)
        m.record_success(
            Lane.BRAINSTEM,
            _fake_live_backend(),
            _fake_completion(),
            total_latency_ms=300.0,
            cold_start=True,
        )
        rec = store.get_lane_metrics("brainstem")[-1]
        self.assertEqual(rec.prompt_tokens, 5)
        self.assertEqual(rec.completion_tokens, 3)
        self.assertTrue(rec.cold_start)
        self.assertTrue(rec.success)

    def test_record_failure_categorizes_consistently(self) -> None:
        store = MetricsStore()
        m = RunMetrics(store)
        for exc in [
            InferenceUnavailableError("binary not installed"),
        ]:
            m.record_failure(
                Lane.BRAINSTEM, "request-123", exc,
                elapsed_ms=10.0, cold_start=False,
            )
        records = store.get_lane_metrics("brainstem")
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].success)
        self.assertEqual(records[0].error_category, "INFERENCE_UNAVAILABLE")
        # The raw exception message must NOT be stored as the event.
        events = store.get_events()
        for s in events:
            self.assertNotIn("binary not installed", s)


if __name__ == "__main__":
    unittest.main()
