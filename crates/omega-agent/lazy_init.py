"""Lazy initialization for the AshatOS Neural Host.

Everything that does I/O at startup: llama-server binary detection/install,
ZeroGPU startup report, environment probing, and GPU function registration.

All state is guarded by a lock so concurrent ``demo.load()`` from multiple
users does not run init twice.
"""

from __future__ import annotations

import logging
import threading

from env_scanner import scan_and_report, ensure_gpu_registration
from installer import ensure_llama_server

_log = logging.getLogger("ashatos")

# ── Shared state ──────────────────────────────────────────────────────
_llama_bin_path: str | None = None
_init_done: bool = False
_init_error: str | None = None
_init_lock = threading.Lock()


def bin_path() -> str | None:
    return _llama_bin_path


def init_done() -> bool:
    return _init_done


def init_error() -> str | None:
    return _init_error


# ── Internal helpers (referenced via closure below) ───────────────────


def _startup_report() -> None:
    """Send ZeroGPU startup report (idempotent, try/except guarded)."""
    try:
        from spaces.config import Config as _SC
        if not _SC.zero_gpu:
            return
        from spaces.zero import client as _zclient
        _zclient.startup_report()
        _log.info("ZeroGPU startup report sent")
    except Exception as exc:
        _log.warning("startup_report failed (non-fatal): %s", exc)


# ── Public API ────────────────────────────────────────────────────────


def run_lazy_init(
    *,
    gradio_microbrain,
    gradio_mainbrain,
    _fastapi_sync_inference,
) -> str:
    """Idempotent, thread-safe initialization. Call once from ``demo.load``."""
    global _llama_bin_path, _init_done, _init_error

    if _init_done:
        return "ready"
    if not _init_lock.acquire(blocking=False):
        return "init already in progress"
    try:
        if _init_done:
            return "ready"

        _log.info("=" * 60)
        _log.info("AshatOS Neural I/O Host — lazy init start")
        _log.info("=" * 60)

        # 1. Environment probe (fast, no network)
        try:
            scan_and_report()
        except Exception as exc:
            _log.warning("env_scanner: %s", exc)

        # 2. Belt-and-suspenders GPU registration
        for fn, dur in [
            (gradio_microbrain, 60),
            (gradio_mainbrain, 120),
            (_fastapi_sync_inference, 120),
        ]:
            try:
                ensure_gpu_registration(fn, duration=dur)
            except Exception:
                pass

        # 3. llama-server binary install (network I/O, may take seconds)
        try:
            _llama_bin_path = ensure_llama_server()
        except Exception as exc:
            _init_error = f"llama-server install failed: {exc}"
            _log.error(_init_error)

        # 4. ZeroGPU startup report
        _startup_report()

        _init_done = True
        status = (
            f"binary={'ok' if _llama_bin_path else 'missing'}"
            f" error={_init_error}"
        )
        _log.info("lazy init complete: %s", status)
        return status
    finally:
        _init_lock.release()
