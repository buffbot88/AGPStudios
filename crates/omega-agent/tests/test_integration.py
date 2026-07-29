"""Integration test that boots a real llama-server against a tiny GGUF.

This test is **skipped by default**. Set ``RUN_INTEGRATION_TESTS=1`` in the
environment to enable it. It requires:
  * A platform that supports llama-server (Linux / macOS recommended)
  * Network access to download the tiny GGUF from Hugging Face
  * A working ``llama-server`` binary (auto-installed via installer)

The tiny model ``stories260K.gguf`` (1.19 MB) from ``ggml-org/tiny-llamas``
is used so the test completes in seconds, not minutes.
"""

from __future__ import annotations

import os
import time
import unittest
from pathlib import Path


# Only run when explicitly enabled
_RUN_INTEGRATION = bool(int(os.environ.get("RUN_INTEGRATION_TESTS", "0")))


@unittest.skipIf(not _RUN_INTEGRATION, "set RUN_INTEGRATION_TESTS=1 to enable")
class TestIntegrationLlamaServer(unittest.TestCase):
    """End-to-end integration test: install llama-server, download a tiny
    GGUF, start the backend, send a completion, verify the response."""

    @classmethod
    def setUpClass(cls) -> None:
        """Download the tiny GGUF and ensure llama-server is installed."""
        from huggingface_hub import hf_hub_download

        # Download a 1.19 MB test model
        cls.model_path = hf_hub_download(
            repo_id="ggml-org/tiny-llamas",
            filename="stories260K.gguf",
        )
        cls.llama_bin = None

        # Install llama-server
        from installer import ensure_llama_server
        result = ensure_llama_server()
        cls.llama_bin = result.path
        if not cls.llama_bin:
            raise unittest.SkipTest(
                "llama-server binary could not be installed; "
                f"failure={result.failure_code}: {result.failure_message}"
            )

    def test_binary_is_executable(self) -> None:
        """The installed llama-server binary must exist and be executable."""
        self.assertIsNotNone(self.llama_bin)
        self.assertTrue(Path(self.llama_bin).is_file(),
                        f"binary not found at {self.llama_bin}")

    def test_binary_prints_version(self) -> None:
        """Running ``llama-server --version`` must succeed."""
        import subprocess
        result = subprocess.run(
            [self.llama_bin, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0,
                         f"version command failed: {result.stderr[:200]}")

    def test_backend_smoke(self) -> None:
        """Boot llama-server with the tiny GGUF, send a completion, verify.

        This exercises the full subprocess lifecycle without hitting
        ZeroGPU: starts on a random high port, waits for /health, sends
        one chat completion, checks the response shape, terminates.
        """
        import json
        import socket
        import subprocess
        import threading

        import requests

        from llama_stderr_parser import LlamaServerStderrParser

        # Pick a random high port to avoid conflicts
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        cmd = [
            self.llama_bin,
            "--host", "127.0.0.1",
            "--port", str(port),
            "-m", self.model_path,
            "-c", "512",
            "-t", "1",
            "-b", "64",
            "-ngl", "0",  # CPU only for test reproducibility
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        self.addCleanup(self._kill_process, proc)

        # Drain stderr in a thread
        parser = LlamaServerStderrParser()
        reader = threading.Thread(
            target=self._stderr_reader, args=(proc.stderr, parser), daemon=True,
        )
        reader.start()

        # Wait for /health (up to 15s)
        deadline = time.monotonic() + 15.0
        healthy = False
        while time.monotonic() < deadline:
            try:
                resp = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
                if resp.status_code < 500:
                    healthy = True
                    break
            except requests.RequestException:
                pass
            time.sleep(0.25)

        self.assertTrue(healthy,
                        "llama-server did not become healthy within 15s")

        # Send a chat completion
        resp = requests.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 16,
                "temperature": 0,
            },
            timeout=10,
        )
        self.assertEqual(resp.status_code, 200)

        data = resp.json()
        self.assertIn("choices", data)
        self.assertGreater(len(data["choices"]), 0)
        self.assertIn("message", data["choices"][0])
        self.assertIn("content", data["choices"][0]["message"])
        self.assertIsInstance(data["choices"][0]["message"]["content"], str)
        self.assertGreater(len(data["choices"][0]["message"]["content"]), 0)

        # Verify usage stats
        self.assertIn("usage", data)
        self.assertIn("prompt_tokens", data["usage"])
        self.assertIn("completion_tokens", data["usage"])
        self.assertGreater(data["usage"]["prompt_tokens"], 0)

        # Finalize parser diagnostics
        parse_result = parser.finalize()
        self.assertGreater(parse_result.raw_lines_kept, 0,
                           "stderr parser should have captured lines")

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _stderr_reader(stderr, parser) -> None:
        """Read stderr lines into the parser until EOF."""
        if stderr is None:
            return
        try:
            for raw_line in iter(stderr.readline, b""):
                text = raw_line.decode("utf-8", errors="replace")
                parser.feed(text)
        except Exception:
            pass
        finally:
            try:
                stderr.close()
            except Exception:
                pass

    @staticmethod
    def _kill_process(proc) -> None:
        """Safely terminate the subprocess."""
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
