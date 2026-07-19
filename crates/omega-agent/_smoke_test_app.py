"""Local smoke test for the rewritten app.py.

Uses *monkey-patch* on the real huggingface_hub (not full module replacement,
which broke gradio_client's `from huggingface_hub.utils import ...`).
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import types
import urllib.request


def _setup_env() -> None:
    """Stub the bits of app.py that would otherwise do real network I/O."""
    # 1) huggingface_hub: monkey-patch just `hf_hub_download` so the
    #    underlying _real_ huggingface_hub.utils package remains importable
    #    (gradio_client depends on it).
    try:
        import huggingface_hub  # noqa: F401 -- real package must exist
        import huggingface_hub as _real
        _real.hf_hub_download = lambda **kwargs: "/tmp/fake-model.gguf"
    except ImportError:
        # Fall back to a stub only if huggingface_hub is genuinely absent
        # (rare on this project's env).
        hf = types.ModuleType("huggingface_hub")
        hf.hf_hub_download = lambda **kwargs: "/tmp/fake-model.gguf"
        hf.SpaceHardware = type("SpaceHardware", (), {})
        hf.SpaceStage = type("SpaceStage", (), {})
        sys.modules["huggingface_hub"] = hf

    # 2) spaces: only stub if not already importable.
    try:
        import spaces  # noqa: F401
    except ImportError:
        spaces = types.ModuleType("spaces")
        class _GPU:
            def __call__(self, fn=None, **kwargs):
                if fn is not None:
                    return fn
                return lambda f: f
        spaces.GPU = _GPU()
        sys.modules["spaces"] = spaces

    # 3) installer facade: stub `ensure_llama_server` so startup() doesn't
    #    try real HF downloads or GitHub release lookups.
    import installer
    installer.ensure_llama_server = lambda: installer.InstallerResult(
        path="/tmp/fake-llama-server",
        failure_code=None,
        failure_message=None,
    )


def _import_app():
    _setup_env()
    import app  # noqa: F401 -- imported for side effects
    return app


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _curl_with_retry(url: str, *, attempts: int = 6, wait_s: float = 1.0):
    """Retry on transient connection errors (uvicorn cold-start handshake races).

    Returns (status_int, body_str). -1 status means even the retries failed.
    """
    last_exc = ""
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5.0) as resp:
                body = resp.read().decode("utf-8", errors="replace")[:400]
                return resp.status, body
        except Exception as e:
            last_exc = f"{type(e).__name__}: {e}"
            time.sleep(wait_s)
    return -1, f"after {attempts} retries: {last_exc}"


def _wait_until_ready(base_url: str, *, attempts: int = 30, wait_s: float = 0.5) -> bool:
    """Poll an idempotent readiness endpoint until 2xx or attempts exhausted."""
    probe_url = f"{base_url}/openapi.json"  # FastAPI auto-doc, no Gradio involvement
    for i in range(attempts):
        try:
            with urllib.request.urlopen(probe_url, timeout=2.0) as resp:
                if 200 <= resp.status < 300:
                    return True
        except Exception as e:
            print(f"   readiness probe attempt {i+1}/{attempts}: {type(e).__name__}")
            time.sleep(wait_s)
    return False


def main() -> None:
    print("== STEP 1: import app.py (with mocks) ==")
    app = _import_app()
    # After the pure-FastAPI pivot, app.py's `app` attribute is a
    # plain FastAPI instance (no Gradio Mount, no `app.app` double-
    # indirection). Duck-type both shapes: prefer `app.app.routes` if
    # the module has a nested .app with .routes (legacy Gradio Mount
    # path); otherwise use `app.routes` directly.
    candidate = (
        app.app
        if hasattr(app, "app") and hasattr(getattr(app, "app", None), "routes")
        else app
    )
    if not hasattr(candidate, "routes"):
        sys.exit("FATAL: app.py didn't expose a FastAPI with .routes")
    print(f"   app imported OK; FastAPI class = {type(candidate).__name__}")

    health_present = any(
        r.__class__.__name__ in ("APIRoute", "Route")
        and getattr(r, "path", None) == "/health"
        for r in candidate.routes
    )
    print(f"   /health on FastAPI routes? {health_present}")
    assert health_present, "Defensive /health assert stripped -- smoke test fails."

    print()
    print("== STEP 2: spin up uvicorn on a free port ==")
    port = _find_free_port()
    print(f"   using port {port}")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    print()
    print("== STEP 3: wait for uvicorn readiness (browser-style polling) ==")
    base = f"http://127.0.0.1:{port}"
    if not _wait_until_ready(base):
        out = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
        print("FATAL: uvicorn didn't become ready in time")
        print(out)
        proc.terminate()
        sys.exit(2)
    print("   uvicorn is ready (probed /openapi.json)")

    try:
        print()
        print("== STEP 4: hit the four public endpoints (with retry on race) ==")
        all_ok = True
        for ep in ("/health", "/v1/models", "/api/public_status", "/api/public_metrics"):
            status, body = _curl_with_retry(f"{base}{ep}")
            ok = status == 200
            all_ok = all_ok and ok
            mark = "OK " if ok else "FAIL"
            print(f"   [{mark}] GET {ep} -> {status}")
            print(f"         body = {body}")
        print()
        print("OVERALL:", "PASS" if all_ok else "FAIL")
        if not all_ok:
            sys.exit(2)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print()
    print("== SMOKE TEST COMPLETE ==")


if __name__ == "__main__":
    main()
