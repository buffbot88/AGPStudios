"""Environment scanner — probes the HF Spaces runtime for debugging.

Checks what the ZeroGPU infrastructure sees and logs diagnostics
that help figure out why "@spaces.GPU function not detected" errors
occur. Safe to import anywhere (zero heavy dependencies).
"""

from __future__ import annotations

import logging
import os
import sys
import types

_log = logging.getLogger("ashatos.env_scanner")


def scan_and_report() -> dict[str, object]:
    """Probe the runtime environment and return diagnostics.

    Logs the findings at INFO level. Returns the dict for programmatic use.
    """
    info: dict[str, object] = {}

    # ── Python / platform ─────────────────────────────────────────────
    info["python_version"] = sys.version
    info["platform"] = sys.platform

    # ── Process identity ──────────────────────────────────────────────
    info["pid"] = os.getpid()
    info["ppid"] = os.getppid()

    # ── Key env vars (sanitised) ──────────────────────────────────────
    for key in sorted(os.environ):
        if any(ignore in key.lower() for ignore in ("key", "token", "secret", "auth", "password")):
            continue  # skip secrets
        val = os.environ[key]
        if len(val) > 500:
            val = val[:200] + "..." + val[-50:]
        info[f"env:{key}"] = val

    # ── spaces module ─────────────────────────────────────────────────
    spaces_mod = sys.modules.get("spaces")
    if spaces_mod is None:
        info["spaces"] = "NOT_IMPORTED"
    else:
        info["spaces"] = type(spaces_mod).__name__
        info["spaces_file"] = getattr(spaces_mod, "__file__", None)
        # Check for the GPU attribute
        gpu_attr = getattr(spaces_mod, "GPU", None)
        info["spaces_GPU_type"] = type(gpu_attr).__name__ if gpu_attr is not None else "MISSING"
        # Try to read config
        try:
            from spaces.config import Config as SpacesConfig
            info["zero_gpu_flag"] = SpacesConfig.zero_gpu
            info["gradio_auto_wrap"] = SpacesConfig.gradio_auto_wrap
        except Exception as exc:
            info["spaces_config_error"] = str(exc)
        # Check decorated_cache
        try:
            from spaces.zero.decorator import decorated_cache
            info["decorated_cache_size"] = len(decorated_cache)
            info["decorated_functions"] = [
                fn.__name__ for fn in decorated_cache
                if hasattr(fn, "__name__")
            ][:20]
        except Exception as exc:
            info["decorated_cache_error"] = str(exc)

    # ── gradio module ─────────────────────────────────────────────────
    gr_mod = sys.modules.get("gradio")
    if gr_mod is not None:
        info["gradio_version"] = getattr(gr_mod, "__version__", "?")
        info["gradio_file"] = getattr(gr_mod, "__file__", None)
    else:
        info["gradio"] = "NOT_IMPORTED"

    # ── hf-gradio module ──────────────────────────────────────────────
    hf_mod = sys.modules.get("hf_gradio")
    if hf_mod is not None:
        info["hf_gradio_file"] = getattr(hf_mod, "__file__", None)
        hv = getattr(hf_mod, "__version__", None)
        info["hf_gradio_version"] = hv
    else:
        info["hf_gradio"] = "NOT_IMPORTED"

    # ── Log ───────────────────────────────────────────────────────────
    _log.info("=== Environment Scanner Report ===")
    for key, val in sorted(info.items()):
        _log.info("  %s = %s", key, val)
    _log.info("=== End Report ===")

    return info


def is_zero_gpu_env() -> bool:
    """Check if we appear to be running in a ZeroGPU environment."""
    if os.environ.get("SPACES_ZERO_GPU", "").lower() in ("1", "t", "true"):
        return True
    try:
        from spaces.config import Config
        return bool(Config.zero_gpu)
    except Exception:
        return False


def ensure_gpu_registration(func, *, duration: int = 120) -> None:
    """Manually register a function with the spaces ZeroGPU system.

    Called after ``@spaces.GPU(duration=N)`` decorator as a belt-and-suspenders
    measure: if the decorator syntax wasn't picked up by the static scanner,
    this runtime call ensures the function is in the ``decorated_cache``.
    """
    spaces_mod = sys.modules.get("spaces")
    if spaces_mod is None:
        _log.debug("ensure_gpu_registration: spaces not imported — skip")
        return
    gpu_attr = getattr(spaces_mod, "GPU", None)
    if gpu_attr is None:
        _log.debug("ensure_gpu_registration: spaces.GPU not found — skip")
        return
    # Register the function. The _GPU function checks the cache internally
    # so this is safe to call even if the decorator already wrapped it.
    try:
        result = gpu_attr(func, duration=duration)
        _log.info(
            "ensure_gpu_registration: registered %s (duration=%s) → %s",
            func.__name__, duration, type(result).__name__,
        )
    except Exception as exc:
        _log.warning(
            "ensure_gpu_registration: failed for %s: %s",
            func.__name__, exc,
        )
