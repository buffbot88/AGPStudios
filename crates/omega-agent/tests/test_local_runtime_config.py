"""Tests for the local runtime and public telemetry contract."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class TestLocalConfigContract(unittest.TestCase):
    def test_example_uses_brainstem_key_only(self) -> None:
        path = Path(__file__).resolve().parents[1] / "server-config.example.json"
        data = json.loads(path.read_text())
        self.assertIn("BRAINSTEM_KEY", data)
        self.assertNotIn("api_key", data)
        self.assertNotIn("hf_token", data)
        self.assertNotIn("installer", data)
        self.assertNotIn("repo", data.get("model", {}))
        self.assertNotIn("revision", data.get("model", {}))

    def test_runtime_config_exposes_brainstem_key(self) -> None:
        from config import CONFIG
        self.assertTrue(hasattr(CONFIG, "brainstem_key"))
        self.assertFalse(hasattr(CONFIG, "api_key"))
        self.assertFalse(hasattr(CONFIG, "hf_token"))


class TestLocalInstaller(unittest.TestCase):
    def test_missing_binary_is_reported_without_network(self) -> None:
        from installer import InstallerResult
        self.assertFalse(InstallerResult(failure_code="BINARY_INSTALL_FAILED").ok)

    def test_installer_result_success_shape(self) -> None:
        from installer import InstallerResult
        result = InstallerResult(path="/usr/local/libexec/ashat-neural-host/llama-server")
        self.assertTrue(result.ok)
        self.assertEqual(result.to_dict()["path"], result.path)


class TestMetricsStoreLocalFailureTracking(unittest.TestCase):
    def test_local_model_failure_code_is_publicly_safe(self) -> None:
        from datetime import datetime, timezone
        from metrics_store import MetricRecord, MetricsStore
        from public_snapshot import PUBLIC_ERROR_MESSAGES, PublicSnapshot, RuntimeState
        from domain import LANE_CONFIG

        store = MetricsStore()
        store.record(MetricRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            lane="brainstem",
            success=False,
            error_category="LOCAL_MODEL_UNAVAILABLE",
        ))
        status = PublicSnapshot.from_metrics(
            store,
            RuntimeState(0.0, True, "/local/llama-server"),
            LANE_CONFIG,
        ).render_status()
        brain = status["lanes"]["brainstem"]
        self.assertEqual(brain["last_failure_code"], "LOCAL_MODEL_UNAVAILABLE")
        self.assertEqual(
            brain["reason_message"],
            PUBLIC_ERROR_MESSAGES["LOCAL_MODEL_UNAVAILABLE"],
        )
        self.assertEqual(brain["lane_state"], "waking")


if __name__ == "__main__":
    unittest.main()
