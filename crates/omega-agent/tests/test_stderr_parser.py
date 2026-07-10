"""Tests for LlamaServerStderrParser.

The parser is the seam between llama-server's streaming stderr and the
backend-launch decision. Pinning its behavior here keeps the launcher
free of brittle string-matching and gives a regression surface for any
future llama.cpp log-line change.
"""

from __future__ import annotations

import unittest

from llama_stderr_parser import LlamaServerStderrParser


SAMPLE_CUDA_LOG = """
ggml: loading model
llama_model_loader: loaded meta model
llm_load_tensors: offloading 32 repeating layers to GPU
llm_load_tensors:        CPU buffer size =  128.00 MiB
llm_load_tensors:      CUDA0 buffer size = 7338.64 MiB
llm_load_tensors: offloaded 32/33 layers to GPU
""".strip().splitlines()


SAMPLE_CPU_LOG = """
ggml: loading model
llama_model_loader: loaded meta model
llm_load_tensors: offloading 0 repeating layers to GPU
llm_load_tensors:        CPU buffer size = 8192.00 MiB
""".strip().splitlines()


SAMPLE_HYBRID_LOG = """
llm_load_tensors:        CPU buffer size =  256.00 MiB
llm_load_tensors:      CUDA0 buffer size = 4096.00 MiB
llm_load_tensors: offloaded 24/33 layers to GPU
""".strip().splitlines()


SAMPLE_FAILED_OFFLOAD = """
llm_load_tensors: offloading 32 repeating layers to GPU
llm_load_tensors:        CPU buffer size = 8192.00 MiB
# (no "offloaded N/M layers to GPU" line — n_gpu_layers was set but
# the loader silently fell back to CPU, e.g. CUDA not available)
""".strip().splitlines()


class TestParser(unittest.TestCase):

    def test_cuda_path_parses_mode_and_layers(self) -> None:
        p = LlamaServerStderrParser()
        p.feed_many(SAMPLE_CUDA_LOG)
        result = p.finalize()
        self.assertEqual(result.parsed_mode, "cuda")
        self.assertEqual(result.offloaded_layers, (32, 33))
        self.assertEqual(result.gpu_layers_requested, 0)
        self.assertTrue(result.offload_succeeded)
        # LiveBackend fields map correctly.
        fields = result.to_live_backend_fields()
        self.assertEqual(fields["backend_mode"], "cuda")
        self.assertTrue(fields["gpu_offload_verified"])
        self.assertEqual(fields["gpu_offload_layers"], (32, 33))

    def test_cpu_only_path_does_not_claim_offload(self) -> None:
        p = LlamaServerStderrParser()
        p.feed_many(SAMPLE_CPU_LOG)
        result = p.finalize()
        self.assertEqual(result.parsed_mode, "cpu")
        self.assertIsNone(result.offloaded_layers)
        self.assertFalse(result.offload_succeeded)

    def test_hybrid_cpu_and_cuda_path_picks_cuda(self) -> None:
        # When llama.cpp sees both CPU and CUDA buffers, the highest-
        # capability backend wins — and CUDA is what's actually offloaded.
        p = LlamaServerStderrParser()
        p.feed_many(SAMPLE_HYBRID_LOG)
        result = p.finalize()
        self.assertEqual(result.parsed_mode, "cuda")
        self.assertEqual(result.offloaded_layers, (24, 33))
        self.assertIn("CPU", result.backends_seen)
        self.assertIn("CUDA0", result.backends_seen)

    def test_failed_offload_detected_via_missing_line(self) -> None:
        # The buffer-size line is CPU-only and the "offloaded N/M" line
        # never appears. The launcher must raise when n_gpu_layers > 0.
        p = LlamaServerStderrParser()
        p.feed_many(SAMPLE_FAILED_OFFLOAD)
        result = p.finalize()
        self.assertEqual(result.parsed_mode, "cpu")
        self.assertIsNone(result.offloaded_layers)
        self.assertFalse(result.offload_succeeded)

    def test_offloaded_layers_immutable(self) -> None:
        # First valid offload line wins; later bogus lines don't overwrite.
        p = LlamaServerStderrParser()
        p.feed("llm_load_tensors: offloaded 32/33 layers to GPU")
        p.feed("llm_load_tensors: offloaded 9999/9999 layers to GPU")
        result = p.finalize()
        self.assertEqual(result.offloaded_layers, (32, 33))

    def test_invalid_lines_silently_stored(self) -> None:
        p = LlamaServerStderrParser()
        p.feed("this is just informational")
        p.feed("llm_load_tensors: weird arbitrary line")
        result = p.finalize()
        self.assertEqual(result.parsed_mode, "unknown")
        self.assertIsNone(result.offloaded_layers)
        self.assertEqual(result.raw_lines_kept, 2)

    def test_empty_line_is_ignored(self) -> None:
        p = LlamaServerStderrParser()
        p.feed("")
        p.feed("   ")
        self.assertEqual(p.finalize().raw_lines_kept, 1)  # "   " stored as one

    def test_unknown_backend_tag_falls_through_lowercased(self) -> None:
        p = LlamaServerStderrParser()
        p.feed("llm_load_tensors: ROCm buffer size =  2048.00 MiB")
        result = p.finalize()
        self.assertEqual(result.parsed_mode, "rocm")

    def test_cuda_then_cpu_does_not_flip_back(self) -> None:
        # If CUDA was detected first and CPU buffer shows up later, we keep
        # the higher-capability mode — this is what llama.cpp's loader
        # actually does in multi-backend setups.
        p = LlamaServerStderrParser()
        p.feed("llm_load_tensors:      CUDA0 buffer size =  4096.00 MiB")
        p.feed("llm_load_tensors:        CPU buffer size =   256.00 MiB")
        self.assertEqual(p.finalize().parsed_mode, "cuda")


if __name__ == "__main__":
    unittest.main()
