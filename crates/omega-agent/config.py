"""Local JSON configuration for the Oracle BrainStem host."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_PROJECT_ROOT = Path(__file__).resolve().parent
_DEFAULT_BINARY = str(_PROJECT_ROOT / "bin" / "llama-server")
_DEFAULT_MODEL_PATH = str(
    _PROJECT_ROOT / "models" / "LFM2.5-1.2B-Instruct-Q8_0.gguf"
)


@dataclass(frozen=True)
class ServerConfig:
    """Validated settings loaded from the local ``server-config.json``."""

    brainstem_key: str = ""
    web_only: bool = False
    log_level: str = "INFO"
    llama_server_path: str = _DEFAULT_BINARY
    llama_server_port: int = 18080
    n_threads: int = 1
    n_batch: int = 32
    queue_limit: int = 1
    public_refresh_seconds: int = 10
    model_file: str = "LFM2.5-1.2B-Instruct-Q8_0.gguf"
    model_path: str = _DEFAULT_MODEL_PATH
    context: int = 4096
    max_tokens: int = 1024
    metrics_persist_path: str = str(_PROJECT_ROOT / "logs" / "metrics.jsonl")
    config_path: str = ""


_CONFIG_CANDIDATES = (
    _PROJECT_ROOT / "server-config.json",
    Path.cwd() / "server-config.json",
)


def _read_config_file() -> tuple[dict[str, Any], str]:
    """Read the project-local configuration without environment fallbacks."""
    for path in _CONFIG_CANDIDATES:
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid server config at {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"server config at {path} must be a JSON object")
        return data, str(path)
    return {}, ""


def _text(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _nonnegative_int(value: Any, default: int) -> int:
    return _positive_int(value, default, minimum=0)


def _boolean(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _nested(
    data: dict[str, Any], section: str, key: str, default: Any,
) -> Any:
    group = data.get(section)
    if isinstance(group, dict) and key in group:
        return group[key]
    return default


def _load() -> ServerConfig:
    data, path = _read_config_file()
    return ServerConfig(
        brainstem_key=_text(data.get("BRAINSTEM_KEY")),
        web_only=_boolean(data.get("web_only")),
        log_level=_text(_nested(data, "logging", "level", "INFO"), "INFO").upper(),
        llama_server_path=_text(
            _nested(data, "server", "binary_path", _DEFAULT_BINARY),
            _DEFAULT_BINARY,
        ),
        llama_server_port=_positive_int(_nested(data, "server", "port", 18080), 18080),
        n_threads=_positive_int(_nested(data, "server", "threads", 1), 1),
        n_batch=_positive_int(_nested(data, "server", "batch", 32), 32),
        queue_limit=_positive_int(_nested(data, "inference", "queue_limit", 1), 1),
        public_refresh_seconds=_positive_int(
            _nested(data, "inference", "public_refresh_seconds", 10), 10,
        ),
        model_file=_text(
            _nested(data, "model", "file", "LFM2.5-1.2B-Instruct-Q8_0.gguf"),
            "LFM2.5-1.2B-Instruct-Q8_0.gguf",
        ),
        model_path=_text(
            _nested(data, "model", "path", _DEFAULT_MODEL_PATH),
            _DEFAULT_MODEL_PATH,
        ),
        context=_positive_int(_nested(data, "inference", "context", 4096), 4096),
        max_tokens=_positive_int(
            _nested(data, "inference", "max_tokens", 1024), 1024,
        ),
        metrics_persist_path=_text(
            _nested(data, "metrics", "persist_path", str(_PROJECT_ROOT / "logs" / "metrics.jsonl")),
            str(_PROJECT_ROOT / "logs" / "metrics.jsonl"),
        ),
        config_path=path,
    )


CONFIG = _load()
