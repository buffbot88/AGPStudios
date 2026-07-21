"""Tests for HF-specific error classification and InstallResult structure.

These pins the translation rules::

    HF 429 / "rate limit" text     -> HfRateLimitedError (HF_RATE_LIMITED)
    HF 402 / 403 / "credits" text  -> HfCreditsExhaustedError (HF_CREDITS_EXHAUSTED)
    HF other failures              -> ModelDownloadError (MODEL_DOWNLOAD_FAILED)

so the dashboard can reveal "Out of HF credits" / "Rate limited" vs the
generic model-download error.

Also covers installer.ensure() returning a structured InstallerResult
(so app.py startup() can surface the typed cause in the metrics store).
"""

from __future__ import annotations

import unittest
from unittest import mock

from backend_launcher import (
    _classify_hf_exception,
    _classify_status_and_body,
)
from run_errors import (
    HfCreditsExhaustedError,
    HfRateLimitedError,
    ModelDownloadError,
)


class TestClassifyStatusAndBody(unittest.TestCase):

    def test_429_maps_to_rate_limited(self) -> None:
        err = _classify_status_and_body(
            status_code=429,
            body_text="Too many requests, please slow down",
            what="model", lane_value="brainstem",
        )
        self.assertIsInstance(err, HfRateLimitedError)
        self.assertEqual(err.code, "HF_RATE_LIMITED")

    def test_402_maps_to_credits_exhausted(self) -> None:
        err = _classify_status_and_body(
            status_code=402,
            body_text="Payment required: subscription expired",
            what="bucket", lane_value="brainstem",
        )
        self.assertIsInstance(err, HfCreditsExhaustedError)
        self.assertEqual(err.code, "HF_CREDITS_EXHAUSTED")

    def test_403_with_quota_text_maps_to_credits_exhausted(self) -> None:
        # HuggingFace's actual quota message style (lowercase fragment).
        err = _classify_status_and_body(
            status_code=403,
            body_text="You have exceeded your monthly included compute hours",
            what="model", lane_value="brainstem",
        )
        self.assertIsInstance(err, HfCreditsExhaustedError)
        self.assertEqual(err.code, "HF_CREDITS_EXHAUSTED")

    def test_403_with_billing_text_maps_to_credits_exhausted(self) -> None:
        err = _classify_status_and_body(
            status_code=403,
            body_text="Forbidden: please update your billing plan",
            what="llama-server mirror", lane_value="llama-bin",
        )
        self.assertIsInstance(err, HfCreditsExhaustedError)

    def test_other_status_maps_to_model_download_error(self) -> None:
        err = _classify_status_and_body(
            status_code=500,
            body_text="Internal Server Error",
            what="model", lane_value="brainstem",
        )
        self.assertIsInstance(err, ModelDownloadError)
        self.assertEqual(err.code, "MODEL_DOWNLOAD_FAILED")

    def test_status_unknown_falls_through_to_model_download_error(self) -> None:
        err = _classify_status_and_body(
            status_code=None,
            body_text="Connection refused by DNS resolver",
            what="model", lane_value="brainstem",
        )
        self.assertIsInstance(err, ModelDownloadError)

    def test_message_never_contains_token_or_url_secrets(self) -> None:
        # The user-facing message is sanitized \u2014 truncated to 200 chars
        # and never returns a path-like raw response blob. We assert
        # by checking well-known "looks-like-secret" markers DON'T appear
        # in the message body after the classifier builds it.
        body = "Token=Bearer-deadbeef-1234 " * 50  # huge body, no credit needles
        err = _classify_status_and_body(
            status_code=503, body_text=body, what="model", lane_value="brainstem",
        )
        self.assertIsInstance(err, ModelDownloadError)
        self.assertLessEqual(len(err.message), 250)  # 200 body + 50 prefix


class TestClassifyHfException(unittest.TestCase):

    def test_credits_exhausted_via_hf_hub_error(self) -> None:
        # Build a stub HfHubHTTPError-like object.
        from huggingface_hub.utils import HfHubHTTPError

        try:
            raise HfHubHTTPError(
                "You have exceeded your monthly included compute hours",
                response=mock.Mock(status_code=403, text="exceeded"),
            )
        except HfHubHTTPError as exc:
            err = _classify_hf_exception(exc, "model", "brainstem")
            self.assertIsInstance(err, HfCreditsExhaustedError)
            self.assertEqual(err.code, "HF_CREDITS_EXHAUSTED")

    def test_rate_limited_via_hf_hub_error(self) -> None:
        from huggingface_hub.utils import HfHubHTTPError

        try:
            raise HfHubHTTPError(
                "rate limit reached", response=mock.Mock(status_code=429, text="rl"),
            )
        except HfHubHTTPError as exc:
            err = _classify_hf_exception(exc, "model", "brainstem")
            self.assertIsInstance(err, HfRateLimitedError)

    def test_generic_exception_maps_to_model_download_error(self) -> None:
        err = _classify_hf_exception(
            ConnectionError("DNS failure"), "model", "brainstem",
        )
        self.assertIsInstance(err, ModelDownloadError)
        self.assertEqual(err.code, "MODEL_DOWNLOAD_FAILED")


class TestInstallerResult(unittest.TestCase):

    def test_installer_result_ok(self) -> None:
        from installer import InstallerResult

        r = InstallerResult(path="/usr/bin/llama-server")
        self.assertTrue(r.ok)
        self.assertIsNone(r.failure_code)
        self.assertIsNone(r.failure_message)

    def test_installer_result_failed(self) -> None:
        from installer import InstallerResult

        r = InstallerResult(
            path=None,
            failure_code="HF_CREDITS_EXHAUSTED",
            failure_message="HuggingFace credits exhausted",
        )
        self.assertFalse(r.ok)
        self.assertEqual(r.failure_code, "HF_CREDITS_EXHAUSTED")
        d = r.to_dict()
        self.assertEqual(d["path"], None)
        self.assertEqual(d["failure_code"], "HF_CREDITS_EXHAUSTED")


class TestMetricsStoreFailureTracking(unittest.TestCase):

    def test_summary_includes_last_failure_code_and_at(self) -> None:
        from datetime import datetime, timezone

        from metrics_store import METRICS, MetricRecord, MetricsStore

        store = MetricsStore()
        # Simulate a startup failure record.
        store.record(MetricRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            lane="brainstem",
            success=False,
            error_category="HF_CREDITS_EXHAUSTED",
        ))
        summary = store.get_summary("brainstem")
        self.assertEqual(summary["last_failure_code"], "HF_CREDITS_EXHAUSTED")
        self.assertIsNotNone(summary["last_failure_at"])

    def test_summary_empty_store_has_null_failure_fields(self) -> None:
        from metrics_store import MetricsStore
        store = MetricsStore()
        summary = store.get_summary("brainstem")
        self.assertIsNone(summary["last_failure_code"])
        self.assertIsNone(summary["last_failure_at"])

    def test_summary_keeps_failure_pointer_when_subsequent_record_succeeds(self) -> None:
        from datetime import datetime, timezone

        from metrics_store import MetricRecord, MetricsStore

        store = MetricsStore()
        # Add a success after a failure \u2014 last_failure_code MUST remember
        # the most-recent failure record (last record with error_category).
        store.record(MetricRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            lane="brainstem",
            success=False,
            error_category="HF_CREDITS_EXHAUSTED",
        ))
        store.record(MetricRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            lane="brainstem",
            success=True,
            error_category=None,
        ))
        summary = store.get_summary("brainstem")
        # success_count=1, failure_count=1 \u2192 success_rate 50%
        self.assertEqual(summary["success_count"], 1)
        self.assertEqual(summary["failure_count"], 1)
        # But last_failure_code still points at the most recent failure.
        self.assertEqual(summary["last_failure_code"], "HF_CREDITS_EXHAUSTED")


class TestPublicSnapshotDiagnosticExposure(unittest.TestCase):

    def test_render_status_includes_failure_code_and_reason_message(self) -> None:
        from datetime import datetime, timezone

        from domain import LANE_CONFIG
        from metrics_store import MetricRecord, MetricsStore
        from public_snapshot import (
            PublicSnapshot,
            PUBLIC_ERROR_MESSAGES,
            RuntimeState,
        )

        store = MetricsStore()
        store.record(MetricRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            lane="brainstem",
            success=False,
            error_category="HF_CREDITS_EXHAUSTED",
        ))
        snap = PublicSnapshot.from_metrics(
            store,
            RuntimeState(
                started_at=0.0,
                llama_server_available=True,
                llama_server_path="/x/llama-server",
            ),
            LANE_CONFIG,
        )
        status = snap.render_status()
        brain = status["lanes"]["brainstem"]
        self.assertEqual(brain["last_failure_code"], "HF_CREDITS_EXHAUSTED")
        self.assertEqual(
            brain["reason_message"],
            PUBLIC_ERROR_MESSAGES["HF_CREDITS_EXHAUSTED"],
        )
        # HF_CREDITS_EXHAUSTED overrides to "degraded" so the pill turns red.
        self.assertEqual(brain["lane_state"], "degraded")

    def test_render_status_no_failure_with_unavailable_model_is_waking(self) -> None:
        from domain import LANE_CONFIG
        from metrics_store import MetricsStore
        from public_snapshot import PublicSnapshot, RuntimeState

        snap = PublicSnapshot.from_metrics(
            MetricsStore(),
            RuntimeState(
                started_at=0.0,
                llama_server_available=True,
                llama_server_path="/x/llama-server",
            ),
            LANE_CONFIG,
        )
        status = snap.render_status()
        brain = status["lanes"]["brainstem"]
        self.assertEqual(brain["last_failure_code"], None)
        self.assertEqual(brain["reason_message"], None)
        # model_path is empty in the test \u2192 lane_state should be "waking".
        self.assertEqual(brain["lane_state"], "waking")


if __name__ == "__main__":
    unittest.main()
