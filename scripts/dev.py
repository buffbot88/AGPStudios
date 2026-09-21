#!/usr/bin/env python3
"""Launch the AGP Studios dev stack: mock API (port 3110) + Vite dev server.

Windows-safe replacement for `script & vite &` shell backgrounding (Git Bash
job control can orphan children when the shell exits, and cmd/PowerShell have
no `&` at all). Both children run in this process's process group; Ctrl+C
terminates both. Exits non-zero if either child dies.

Usage:
    python3 scripts/dev.py                 # mock API + vite (foreground)
    python3 scripts/dev.py --no-verification
                                           # pass mock-api flags through
    python3 scripts/dev.py --port 3111 --mock-port 3112
                                           # isolated stack for E2E runs

Options consumed here: --port (vite, default 3101) and --mock-port (default
3110). Every other argument is forwarded to scripts/mock-api.py. The API is
waited for before vite starts, so nothing races the mock's bind.
Requires: python3 on PATH (the same interpreter runs scripts/mock-api.py).
"""
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "apps" / "ashat-hub-web"

exiting = False


def wait_for_port(host: str, port: int, timeout: float) -> bool:
    """Block until host:port accepts TCP connections, or the timeout expires."""
    import socket
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            if _time.monotonic() >= deadline:
                break
            _time.sleep(0.1)
    return False


def terminate(signum, _frame):
    """Forward Ctrl+C/SIGTERM to the whole process group (both children)."""
    global exiting
    if exiting:
        return
    exiting = True
    if os.name == "nt":
        # Windows: no process-group kill via os.killpg; use taskkill on the tree.
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                       capture_output=True)
    else:
        os.killpg(os.getpgrp(), signal.SIGTERM)
    sys.exit(0)


def main() -> int:
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, terminate)   # Ctrl+C
        signal.signal(signal.SIGBREAK, terminate) # Ctrl+Break / taskkill CTRL_CLOSE
    else:
        signal.signal(signal.SIGINT, terminate)
        signal.signal(signal.SIGTERM, terminate)

    # Args: --port/--mock-port configure the stack (used by the E2E runner so
    # tests get their own ports); anything else is forwarded to mock-api.py.
    argv = list(sys.argv[1:])

    def take(flag, default):
        if flag in argv:
            index = argv.index(flag)
            value = argv[index + 1]
            del argv[index:index + 2]
            return int(value)
        return default

    web_port = take("--port", 3101)
    mock_port = take("--mock-port", 3110)

    mock_cmd = [sys.executable, str(ROOT / "scripts" / "mock-api.py"), *argv]
    vite_cmd = ["npm", "run", "dev", "--", "--port", str(web_port), "--strictPort"]
    if os.name == "nt":
        vite_cmd[0] = "npm.cmd"

    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PORT": str(mock_port),
        # Point the vite /api proxy at this run's mock instance.
        "ASHAT_API_TARGET": f"http://127.0.0.1:{mock_port}",
    }
    mock = subprocess.Popen(mock_cmd, env=env)

    # Wait for the API to accept connections before starting vite. Otherwise the
    # first page load can race the mock's bind and log proxy ECONNREFUSED
    # errors (E2E runs saw this), even though the app degrades gracefully.
    if not wait_for_port("127.0.0.1", mock_port, timeout=15.0):
        print(f"[dev] mock API never came up on port {mock_port}; starting vite anyway",
              file=sys.stderr, flush=True)

    vite = subprocess.Popen(vite_cmd, cwd=WEB_DIR, env=env)
    print(f"[dev] mock-api pid={mock.pid}  vite pid={vite.pid}", flush=True)
    print(f"[dev] vite → http://localhost:{web_port}/  (proxy target 127.0.0.1:{mock_port})", flush=True)
    print("[dev] Ctrl+C stops both.", flush=True)

    try:
        # Blocks until either child exits; a dead child means the stack is
        # broken (port conflict, compile error), so shut the other one down.
        while True:
            mock_code = mock.poll()
            vite_code = vite.poll()
            if mock_code is not None or vite_code is not None:
                for proc, name in ((mock, "mock-api"), (vite, "vite")):
                    if proc.poll() is None:
                        proc.terminate()
                dead = "mock-api" if mock_code is not None else "vite"
                code = mock_code if mock_code is not None else vite_code
                print(f"[dev] {dead} exited (code {code}); shutting down.", file=sys.stderr, flush=True)
                return code or 1
            sys.stdout.flush()
            sys.stderr.flush()
            # Poll briefly without letting TimeoutExpired escape the loop;
            # an earlier version returned here and orphaned both children.
            import time as _time
            _time.sleep(0.25)
    except KeyboardInterrupt:
        terminate(signal.SIGINT, None)
        return 0


if __name__ == "__main__":
    sys.exit(main())
