"""Tests for BackendLauncher command construction (single BrainStem lane).

The launcher is the seam between ``app.py`` and the subprocess that runs
``llama-server``. These tests pin the *shape* of the subprocess command
(flags, port, model path, GPU offload) without actually starting a
subprocess or hitting the network. Any future llama.cpp CLI change shows up
as a failed test before it breaks production.
"""

from __future__ import annotations

import unittest
from unittest import mock

from backend_launcher import BackendLauncher
from domain import Lane, lane_cfg


class TestBackendLauncherCommand(unittest.TestCase):

    def setUp(self) -> None:
        self.launcher = BackendLauncher(
            binary_path_getter=lambda: "/fake/llama-server",
            port=18080,
            n_threads=2,
            n_batch=128,
        )

    def test_command_has_expected_flags(self) -> None:
        cmd = self.launcher._build_command(
            "/fake/llama-server",
            "/fake/model.gguf",
            1024,
        )
        # Identity.
        self.assertEqual(cmd[0], "/fake/llama-server")
        # Host/port.
        self.assertIn("--host", cmd)
        self.assertIn("127.0.0.1", cmd)
        self.assertIn("--port", cmd)
        self.assertIn("18080", cmd)
        # Model.
        self.assertIn("-m", cmd)
        self.assertIn("/fake/model.gguf", cmd)
        # Context.
        self.assertIn("-c", cmd)
        self.assertIn("1024", cmd)
        # Threads / batch.
        self.assertIn("-t", cmd)
        self.assertIn("2", cmd)
        self.assertIn("-b", cmd)
        self.assertIn("128", cmd)
        # GPU offload layer explicitly on (max layers).
        self.assertIn("-ngl", cmd)
        self.assertIn("999", cmd)

    def test_command_respects_lane_context(self) -> None:
        cmd = self.launcher._build_command(
            "/fake/llama-server",
            "/fake/main.gguf",
            lane_cfg(Lane.BRAINSTEM)["ctx"],
        )
        self.assertIn(str(lane_cfg(Lane.BRAINSTEM)["ctx"]), cmd)


class TestBackendLauncherBinaryGate(unittest.TestCase):

    def test_launch_raises_if_binary_unavailable(self) -> None:
        launcher = BackendLauncher(
            binary_path_getter=lambda: None,
            port=18080,
            n_threads=2,
            n_batch=128,
        )
        from run_errors import GpuAllocationError
        with self.assertRaises(GpuAllocationError):
            launcher.launch(Lane.BRAINSTEM)

    def test_launch_raises_if_binary_path_unreadable(self) -> None:
        launcher = BackendLauncher(
            binary_path_getter=lambda: "/nonexistent/path/does/not/exist",
            port=18080,
            n_threads=2,
            n_batch=128,
        )
        from run_errors import GpuAllocationError
        with self.assertRaises(GpuAllocationError):
            launcher.launch(Lane.BRAINSTEM)


if __name__ == "__main__":
    unittest.main()
