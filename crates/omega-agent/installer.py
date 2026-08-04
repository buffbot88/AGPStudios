"""Local llama-server availability check.

The Oracle deployment uses a permanently installed native llama-server. This
module deliberately performs no network download.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import CONFIG

_log = logging.getLogger("ashatos")


@dataclass
class InstallerResult:
    """Result compatible with the startup orchestrator."""

    path: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.path is not None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "path": self.path,
            "failure_code": self.failure_code,
            "failure_message": self.failure_message,
        }


def ensure_llama_server() -> InstallerResult:
    """Verify the configured permanent llama-server and return its path."""
    path = Path(CONFIG.llama_server_path)
    if not path.is_file() or not os.access(path, os.X_OK):
        return InstallerResult(
            failure_code="BINARY_INSTALL_FAILED",
            failure_message=f"local llama-server missing at {path}",
        )
    try:
        subprocess.run(
            [str(path), "--version"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
            text=True,
        )
    except Exception as exc:
        _log.error("local llama-server verification failed: %s", exc)
        return InstallerResult(
            failure_code="BINARY_INSTALL_FAILED",
            failure_message="local llama-server failed executable verification",
        )
    return InstallerResult(path=str(path))
