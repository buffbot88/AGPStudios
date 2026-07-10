"""Typed exception hierarchy for the Run pipeline.

Every exceptional path inside the inference pipeline raises one of these —
never a bare ``Exception``. The outermost orchestrator catches them and
converts them to a structured response dict. The mapping of class → error
code (the string we surface in JSON responses) is also defined here so
external callers can rely on stable codes.
"""

from __future__ import annotations

from typing import Final


class RunError(Exception):
    """Base class for the Run pipeline's typed exceptions.

    Each subclass carries a stable, public ``code`` string that callers can
    match against. The orchestrator maps these codes directly into the
    response envelope's ``error.code`` field.
    """

    code: Final[str] = "RUN_ERROR"
    http_status: Final[int] = 500
    retryable: Final[bool] = True

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def to_envelope(self) -> dict:
        """Render the public error envelope for this exception."""
        return {
            "code": self.code,
            "message": self.message[:200],
            "retryable": self.retryable,
        }


class BinaryInstallError(RunError):
    """llama-server binary could not be installed via any tier."""
    code: Final[str] = "BINARY_INSTALL_FAILED"
    http_status: Final[int] = 503
    retryable: Final[bool] = False


class ModelDownloadError(RunError):
    """HF Hub GGUF download failed."""
    code: Final[str] = "MODEL_DOWNLOAD_FAILED"
    http_status: Final[int] = 503
    retryable: Final[bool] = True


class InferenceUnavailableError(RunError):
    """Runtime degraded mode — binary unavailable at request time."""
    code: Final[str] = "INFERENCE_UNAVAILABLE"
    http_status: Final[int] = 503
    retryable: Final[bool] = False


class BackendStartError(RunError):
    """subprocess.Popen failed."""
    code: Final[str] = "BACKEND_START_FAILED"
    http_status: Final[int] = 503
    retryable: Final[bool] = True


class BackendHealthTimeout(RunError):
    """subprocess started but /health did not return 2xx within timeout."""
    code: Final[str] = "SERVER_START_FAILED"
    http_status: Final[int] = 503
    retryable: Final[bool] = True


class GpuAllocationError(RunError):
    """Could not allocate a ZeroGPU slot."""
    code: Final[str] = "GPU_UNAVAILABLE"
    http_status: Final[int] = 503
    retryable: Final[bool] = True


class GpuOffloadVerificationError(RunError):
    """n-gpu-layers did not produce the expected log line."""
    code: Final[str] = "GPU_OFFLOAD_VERIFICATION_FAILED"
    http_status: Final[int] = 503
    retryable: Final[bool] = True


class CompletionTimeout(RunError):
    """POST /v1/chat/completions timed out."""
    code: Final[str] = "INFERENCE_TIMEOUT"
    http_status: Final[int] = 503
    retryable: Final[bool] = True


class CompletionProtocolError(RunError):
    """Backend returned non-200 or unparsable JSON."""
    code: Final[str] = "INFERENCE_FAILED"
    http_status: Final[int] = 503
    retryable: Final[bool] = True


class InvalidModelResponse(RunError):
    """Backend 200 but body shape didn't match OpenAI-compatible."""
    code: Final[str] = "INVALID_MODEL_RESPONSE"
    http_status: Final[int] = 503
    retryable: Final[bool] = True


class CleanupError(RunError):
    """Non-fatal: subprocess terminate failed. Should not bubble up."""
    code: Final[str] = "CLEANUP_ERROR"
    http_status: Final[int] = 500
    retryable: Final[bool] = False


class InvalidRequestError(RunError):
    """Validation pre-check failed (no messages, oversized body, etc.)."""
    code: Final[str] = "INVALID_REQUEST"
    http_status: Final[int] = 400
    retryable: Final[bool] = False


# Mapping used by the orchestrator to assign HTTP status from a response code.
ERROR_CODE_TO_HTTP_STATUS: Final[dict[str, int]] = {
    e.code: e.http_status
    for e in [
        BinaryInstallError, ModelDownloadError, InferenceUnavailableError,
        BackendStartError, BackendHealthTimeout, GpuAllocationError,
        GpuOffloadVerificationError, CompletionTimeout,
        CompletionProtocolError, InvalidModelResponse, CleanupError,
        InvalidRequestError,
    ]
}
