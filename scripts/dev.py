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

    mock_cmd = [sys.executable, str(ROOT / "scripts" / "mock-api.py"), *sys.argv[1:]]
    vite_cmd = ["npm", "run", "dev"]
    if os.name == "nt":
        vite_cmd = ["npm.cmd", "run", "dev"]

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    mock = subprocess.Popen(mock_cmd, env=env)
    vite = subprocess.Popen(vite_cmd, cwd=WEB_DIR, env=env)
    print(f"[dev] mock-api pid={mock.pid}  vite pid={vite.pid}", flush=True)
    print("[dev] vite → http://localhost:3101/  (proxy target 127.0.0.1:3110)", flush=True)
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
            mock.wait(timeout=1)
    except subprocess.TimeoutExpired:
        return 0  # loop again — neither child has exited
    except KeyboardInterrupt:
        terminate(signal.SIGINT, None)
        return 0


if __name__ == "__main__":
    sys.exit(main())
