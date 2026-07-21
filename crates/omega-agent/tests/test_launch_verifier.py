"""Integration-style tests for BackendLauncher.launch's GPU-offload verifier.

These tests don't require a real ``llama-server`` binary. They substitute a
fake ``subprocess.Popen`` that emits canned stderr lines, mock
``/health`` to return True, and assert that the ``LiveBackend`` carries
the parsed-out backend mode + offload-verified flag.

The plumbing verified here:

    . ``launch`` reads stderr (no longer ``DEVNULL``).
    . The stderr parser receives every line.
    . When ``gpu_offload_requested=True`` and the parsed mode is
      cuda/cpu but no offload-N/M line appeared,
      :class:`GpuOffloadVerificationError` is raised.
    . When the parser sees ``offloaded 32/33`` layers, the LiveBackend's
      ``gpu_offload_verified`` is True and ``gpu_offload_layers == (32, 33)``.
    . When the parser sees only CPU buffer lines, ``gpu_offload_verified``
      is False (silently, since gpu_offload_requested was False).
"""

from __future__ import annotations

import io
import unittest
from unittest import mock

import backend_launcher as bl_module
from backend_launcher import BackendLauncher, LiveBackend
from domain import Lane
from run_errors import GpuOffloadVerificationError


def _make_cuda_stderr():
    """Bytes stream that mimics llama-server's loader log on CUDA."""
    lines = [
        b"ggml: loading model\n",
        b"llama_model_loader: loaded meta model\n",
        b"llm_load_tensors: offloading 32 repeating layers to GPU\n",
        b"llm_load_tensors:        CPU buffer size =  128.00 MiB\n",
        b"llm_load_tensors:      CUDA0 buffer size = 7338.64 MiB\n",
        b"llm_load_tensors: offloaded 32/33 layers to GPU\n",
    ]
    return io.BytesIO(b"".join(lines))


def _make_cpu_stderr():
    """Bytes stream that mimics llama-server's loader log on CPU-only."""
    lines = [
        b"ggml: loading model\n",
        b"llm_load_tensors: offloading 0 repeating layers to GPU\n",
        b"llm_load_tensors:        CPU buffer size = 8192.00 MiB\n",
    ]
    return io.BytesIO(b"".join(lines))


class _FakePopen:
    """Mimics subprocess.Popen to the extent BackendLauncher touches it."""

    def __init__(self, stderr_bytes: io.BytesIO) -> None:
        self.stderr = stderr_bytes
        self.stdin = None
        self.stdout = None
        self.pid = 12345
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self._returncode = -15

    def wait(self, timeout=None):
        if self._returncode is None:
            self._returncode = -15
        return self._returncode

    def kill(self):
        self._returncode = -9


class TestLaunchVerifier(unittest.TestCase):

    def _popen_factory(self, stderr_bytes):
        """Builds a Popen-substitute that emits ``stderr_bytes``."""
        def factory(*args, **kwargs):
            assert kwargs.get("stderr") is not None and kwargs["stderr"] != bl_module.subprocess.DEVNULL
            return _FakePopen(stderr_bytes)
        return factory

    def _build_launcher(self):
        return BackendLauncher(
            binary_path_getter=lambda: "/fake/llama-server",
            port=18080,
            n_threads=2,
            n_batch=128,
        )

    def _patch_binary_exists(self):
        """Pretend ``/fake/llama-server`` exists on disk.

        The launcher guards with ``Path(binary).is_file()`` so we patch
        that check; the rest of the launch flow then proceeds normally.
        """
        return mock.patch.object(
            bl_module.Path, "is_file", return_value=True,
        )

    def test_cuda_offload_confirmed_live_backend_fields(self) -> None:
        launcher = self._build_launcher()
        with self._patch_binary_exists(), mock.patch.object(
            launcher, "ensure_model",
            return_value="/fake/model.gguf",
        ), mock.patch(
            "backend_launcher.subprocess.Popen",
            self._popen_factory(_make_cuda_stderr()),
        ), mock.patch.object(
            BackendLauncher, "_wait_for_health", return_value=True,
        ):
            backend = launcher.launch(Lane.BRAINSTEM)

        # Fields derived from PARSED stderr, not env-var inference.
        self.assertEqual(backend.backend_mode, "cuda")
        self.assertTrue(backend.gpu_offload_verified)
        self.assertEqual(backend.gpu_offload_layers, (32, 33))
        self.assertGreaterEqual(len(backend.raw_log_lines), 4)

    def test_cpu_only_offload_not_requested_raises_nothing(self) -> None:
        launcher = self._build_launcher()
        with self._patch_binary_exists(), mock.patch.object(
            launcher, "ensure_model",
            return_value="/fake/model.gguf",
        ), mock.patch(
            "backend_launcher.subprocess.Popen",
            self._popen_factory(_make_cpu_stderr()),
        ), mock.patch.object(
            BackendLauncher, "_wait_for_health", return_value=True,
        ):
            backend = launcher.launch(
                Lane.BRAINSTEM, gpu_offload_requested=False,
            )
        self.assertEqual(backend.backend_mode, "cpu")
        self.assertFalse(backend.gpu_offload_verified)
        self.assertIsNone(backend.gpu_offload_layers)

    def test_offload_requested_but_no_offloaded_line_raises(self) -> None:
        launcher = self._build_launcher()
        empty_stderr = io.BytesIO(
            b"llm_load_tensors:        CPU buffer size = 8192.00 MiB\n"
        )
        with self._patch_binary_exists(), mock.patch.object(
            launcher, "ensure_model",
            return_value="/fake/model.gguf",
        ), mock.patch(
            "backend_launcher.subprocess.Popen",
            self._popen_factory(empty_stderr),
        ), mock.patch.object(
            BackendLauncher, "_wait_for_health", return_value=True,
        ):
            with self.assertRaises(GpuOffloadVerificationError):
                launcher.launch(Lane.BRAINSTEM)

    def test_offloaded_layer_counts_propagate_to_live_backend(self) -> None:
        launcher = self._build_launcher()
        with self._patch_binary_exists(), mock.patch.object(
            launcher, "ensure_model",
            return_value="/fake/model.gguf",
        ), mock.patch(
            "backend_launcher.subprocess.Popen",
            self._popen_factory(_make_cuda_stderr()),
        ), mock.patch.object(
            BackendLauncher, "_wait_for_health", return_value=True,
        ):
            backend = launcher.launch(Lane.BRAINSTEM)
        self.assertEqual(backend.gpu_offload_layers, (32, 33))
        self.assertEqual(backend.lane, Lane.BRAINSTEM)

    def test_parser_state_attached_to_live_backend(self) -> None:
        launcher = self._build_launcher()
        with self._patch_binary_exists(), mock.patch.object(
            launcher, "ensure_model",
            return_value="/fake/model.gguf",
        ), mock.patch(
            "backend_launcher.subprocess.Popen",
            self._popen_factory(_make_cuda_stderr()),
        ), mock.patch.object(
            BackendLauncher, "_wait_for_health", return_value=True,
        ):
            backend = launcher.launch(Lane.BRAINSTEM)
        self.assertIsNotNone(backend.parser)
        self.assertEqual(backend.parser.finalize().parsed_mode, "cuda")


class TestLiveBackendFields(unittest.TestCase):
    """Dataclass-level test: the new fields default cleanly and survive use."""

    def test_default_construction(self) -> None:
        proc = mock.Mock()
        proc.poll.return_value = None
        b = LiveBackend(
            lane=Lane.BRAINSTEM,
            process=proc,
            base_url="http://127.0.0.1:18080/v1",
            model_path="/tmp/m.gguf",
            server_start_ms=120.0,
            model_load_ms=80.0,
            backend_mode="cuda",
            gpu_offload_verified=True,
        )
        self.assertIsNone(b.gpu_offload_layers)
        self.assertEqual(b.raw_log_lines, [])
        self.assertIsNone(b.parser)


if __name__ == "__main__":
    unittest.main()
