"""Focused tests for the public BrainStem dashboard render contract."""

from __future__ import annotations

import time
import unittest

from dashboard import _build_card_html, render_index_html
from domain import LANE_CONFIG
from metrics_store import MetricsStore
from public_snapshot import PublicSnapshot, RuntimeState


class TestDashboardRender(unittest.TestCase):
    def _snapshot(self) -> PublicSnapshot:
        return PublicSnapshot.from_metrics(
            MetricsStore(),
            RuntimeState(
                started_at=time.time(),
                llama_server_available=True,
                llama_server_path="/usr/local/libexec/ashat-neural-host/llama-server",
            ),
            LANE_CONFIG,
        )

    def test_hub_layout_preserves_public_polling_contract(self) -> None:
        html = render_index_html(self._snapshot, refresh_seconds=8)

        for marker in (
            # Page title
            "ASHAT Hub · Neural Host Telemetry",
            # DOM anchors for polling
            'id="status"',
            'id="brainstem"',
            # Polling endpoints
            "/api/dashboard_html",
            "setInterval(tick, REFRESH_MS)",
            # Ashat Hub branding
            "ASHAT",
            "Hub",
            "v5.8",
            "agpstudios.org",
            "Community",
            "Documentation",
            # New design tokens
            "Instrument Serif",
            "JetBrains Mono",
            "backdrop-filter",
            "Private Inference",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)

        self.assertNotIn("Gold Edition", html)
        self.assertNotIn("v4-badge", html)

    def test_dynamic_model_markup_is_escaped(self) -> None:
        html = _build_card_html(
            "brainstem",
            {
                "model": 'model"><script>alert(1)</script>',
                "lane_state": "offline",
                "ctx": 4096,
                "total_requests": 0,
            },
            [],
            "#A78BFA",
            "#EDE9FE",
            "rgba(124,58,237,0.20)",
        )

        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html.lower())

    def test_dynamic_failure_code_is_escaped(self) -> None:
        html = _build_card_html(
            "brainstem",
            {
                "model": "BrainStem",
                "lane_state": "offline",
                "last_failure_code": 'FAIL"><script>alert(1)</script>',
                "reason_message": "Unavailable",
                "ctx": 4096,
                "total_requests": 0,
            },
            [],
            "#A78BFA",
            "#EDE9FE",
            "rgba(124,58,237,0.20)",
        )

        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html.lower())


if __name__ == "__main__":
    unittest.main()
