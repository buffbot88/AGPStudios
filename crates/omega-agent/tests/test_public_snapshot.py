"""Tests for PublicSnapshot — sanitization & projection.

The snapshot is the public surface; if any of these tests fail, the
operator-facing dashboard or one of the ``/api/...`` endpoints has
started leaking data. The rules pinned here:

      * No prompts or generated text.
      * No API keys, no HF tokens.
      * No internal filesystem paths beyond basenames.
      * No request IDs, no Authorization headers.
      * Sanitized events only.
"""

from __future__ import annotations

import time
import unittest
from dataclasses import asdict

from domain import LANE_CONFIG, Lane
from metrics_store import MetricsStore
from public_snapshot import (
    PublicSnapshot,
    RuntimeState,
    _redact_path,
    _redact_string,
)


def _runtime(llama_path: str | None = "/home/foo/.cache/ashatos/bin/llama-server") -> RuntimeState:
    return RuntimeState(
        started_at=time.time() - 100,
        llama_server_available=bool(llama_path),
        llama_server_path=llama_path,
    )


def _snapshot() -> PublicSnapshot:
    return PublicSnapshot.from_metrics(
        MetricsStore(),
        _runtime(),
        LANE_CONFIG,
    )


class TestPublicSnapshotRedaction(unittest.TestCase):
    """Pin sensitive-data redaction across ALL projection methods."""

    def test_path_redaction_to_basename(self) -> None:
        # Full home-relative path → just the binary's basename.
        self.assertEqual(
            _redact_path("/home/foo/.cache/ashatos/bin/llama-server"),
            "llama-server",
        )
        self.assertEqual(
            _redact_path("/Users/x/.cache/ashatos/bin/llama-server"),
            "llama-server",
        )

    def test_none_path_redaction(self) -> None:
        self.assertEqual(_redact_path(None), "(not found)")

    def test_string_redaction_catches_keys(self) -> None:
        for needle in ("x-ashat-key", "HF_TOKEN", "Bearer abc", "Authorization: Basic"):
            with self.subTest(needle=needle):
                self.assertEqual(_redact_string(needle), "<redacted>")
                # Also when needle is part of a longer string.
                self.assertEqual(
                    _redact_string(f"headers={needle}"),
                    "<redacted>",
                )

    def test_string_redaction_passes_normal_text(self) -> None:
        self.assertEqual(_redact_string("microbrain: INFERENCE_UNAVAILABLE"),
                         "microbrain: INFERENCE_UNAVAILABLE")

    def test_string_redaction_caps_length(self) -> None:
        long = "x" * 500
        out = _redact_string(long, max_len=200)
        self.assertLessEqual(len(out), 201)


class TestPublicSnapshotStatus(unittest.TestCase):

    def test_renders_status_dict(self) -> None:
        snap = _snapshot()
        status = snap.render_status()
        # Shape contract.
        for key in ("uptime_seconds", "llama_server_available", "degraded",
                    "llama_server", "lanes", "all_ready"):
            self.assertIn(key, status)
        # Both lanes present.
        self.assertIn("microbrain", status["lanes"])
        self.assertIn("mainbrain", status["lanes"])

    def test_status_path_is_basename_only(self) -> None:
        snap = _snapshot()
        status = snap.render_status()
        # The full home-relative path must NOT appear.
        self.assertNotIn("/home/foo", status["llama_server"])
        self.assertNotIn(".cache/ashatos", status["llama_server"])
        # Only the basename may leak through.
        self.assertEqual(status["llama_server"], "llama-server")

    def test_status_no_request_ids_or_secrets(self) -> None:
        snap = _snapshot()
        body = snap.render_status()
        as_str = repr(body)
        for needle in ("x-ashat-key", "Bearer ", "hf_", "hf-token",
                       ".cache/ashatos", "/home", "/Users"):
            self.assertNotIn(needle, as_str)


class TestPublicSnapshotMetrics(unittest.TestCase):

    def setUp(self) -> None:
        self.store = MetricsStore()
        self.snap = PublicSnapshot.from_metrics(
            self.store, _runtime(), LANE_CONFIG,
        )

    def test_emits_unified_shape(self) -> None:
        body = self.snap.render_metrics()
        for key in ("uptime_seconds", "summaries", "total_events",
                    "recent_events"):
            self.assertIn(key, body)
        self.assertIn("microbrain", body["summaries"])
        self.assertIn("mainbrain", body["summaries"])

    def test_recent_events_capped(self) -> None:
        for i in range(50):
            self.store.add_event(f"microbrain: TEST_EVENT_{i}")
        body = self.snap.render_metrics()
        self.assertLessEqual(len(body["recent_events"]), 20)

    def test_event_redaction_defense_in_depth(self) -> None:
        # Inject a hand-crafted event that LOOKS like a leak — PublicSnap
        # should redact it on read because RunMetrics didn't pre-sanitize.
        self.store.add_event("logged in with x-ashat-key: SECRET")
        body = self.snap.render_metrics()
        all_text = " ".join(e for e in body["recent_events"] if e)
        self.assertNotIn("SECRET", all_text)
        self.assertIn("<redacted>", all_text)


class TestPublicSnapshotFrames(unittest.TestCase):

    def test_frames_provide_dashboard_plot_data(self) -> None:
        snap = _snapshot()
        frames = snap.render_frames()
        self.assertIn("microbrain", frames)
        self.assertIn("mainbrain", frames)
        self.assertIn("events", frames)
        # Each frame is a list of dicts with the plotting columns.
        for lane in ("microbrain", "mainbrain"):
            for f in frames[lane]:
                self.assertIn("timestamp", f)
                self.assertIn("generation_tokens_per_second", f)
                self.assertIn("total_latency_ms", f)


class TestPublicSnapshotHtml(unittest.TestCase):

    def test_html_uses_redacted_path(self) -> None:
        snap = PublicSnapshot.from_metrics(
            MetricsStore(),
            _runtime("/some/long/path/to/the/binary"),
            LANE_CONFIG,
        )
        html = snap.render_html()
        # Path appears only as the basename (``binary`` from this path).
        self.assertIn("<code>binary</code>", html)
        # No full path, no parent component leaks.
        self.assertNotIn("/some/long/path", html)
        self.assertNotIn("/home/foo", html)
        self.assertNotIn("/some", html)
        self.assertNotIn("/long", html)
        self.assertNotIn("/path", html)
        self.assertNotIn("/to/the", html)

    def test_html_marks_degraded_mode(self) -> None:
        snap = PublicSnapshot.from_metrics(
            MetricsStore(),
            _runtime(llama_path=None),
            LANE_CONFIG,
        )
        html = snap.render_html()
        self.assertIn("DEGRADED", html)
        self.assertIn("(not found)", html)


class TestOneShapeThreeConsumers(unittest.TestCase):
    """The headline guarantee: status, metrics, and frames all come from the
    same snapshot instance, so they cannot drift.
    """

    def test_shared_runtime_and_metrics(self) -> None:
        store = MetricsStore()
        rt = _runtime()
        snap = PublicSnapshot.from_metrics(store, rt, LANE_CONFIG)

        s1 = snap.render_status()
        s2 = snap.render_status()
        # Same input → same output (safe to call twice).
        self.assertEqual(s1["uptime_seconds"], s2["uptime_seconds"])
        self.assertEqual(s1["llama_server"], s2["llama_server"])


if __name__ == "__main__":
    unittest.main()
