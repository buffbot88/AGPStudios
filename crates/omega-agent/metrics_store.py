"""Thread-safe in-memory metrics store (extracted from app.py).

Small, dependency-light module. Holds a rolling deque of :class:`MetricRecord`
per lane and a parallel event log. Public consumers ask for summaries.

Optionally persists every record as a JSONL file when ``persist_path``
is set, so operators have history across restarts.
"""

from __future__ import annotations

import json as _json
import os as _os

from config import CONFIG
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any


@dataclass
class MetricRecord:
    timestamp: str = ""
    lane: str = ""
    success: bool = True
    cold_start: bool = False
    server_start_ms: float = 0.0
    model_load_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_per_second: float = 0.0
    generation_tokens_per_second: float = 0.0
    time_to_first_token_ms: float | None = None
    total_latency_ms: float = 0.0
    backend: str = "cpu"
    finish_reason: str = "stop"
    error_category: str | None = None


class MetricsStore:
    """Thread-safe in-memory rolling metrics store.

    When ``persist_path`` is set, each call to :meth:`record` appends a JSON
    line to that file, giving operators history across restarts.
    """

    def __init__(
        self,
        maxlen: int = 500,
        event_maxlen: int = 200,
        persist_path: str | None = None,
    ) -> None:
        self._maxlen = maxlen
        self._brainstem: deque[MetricRecord] = deque(maxlen=maxlen)
        self._events: deque[str] = deque(maxlen=event_maxlen)
        self._lock = Lock()
        self._persist_path = persist_path
        if persist_path:
            _os.makedirs(_os.path.dirname(persist_path) or ".", exist_ok=True)

    def record(self, rec: MetricRecord) -> None:
        with self._lock:
            self._brainstem.append(rec)
        # Persist outside the lock — file I/O is fast enough that holding
        # the lock is fine, and we want atomic append semantics.
        if self._persist_path:
            try:
                with open(self._persist_path, "a") as f:
                    f.write(_json.dumps({
                        "timestamp": rec.timestamp,
                        "lane": rec.lane,
                        "success": rec.success,
                        "cold_start": rec.cold_start,
                        "prompt_tokens": rec.prompt_tokens,
                        "completion_tokens": rec.completion_tokens,
                        "prompt_tokens_per_second": rec.prompt_tokens_per_second,
                        "generation_tokens_per_second": rec.generation_tokens_per_second,
                        "time_to_first_token_ms": rec.time_to_first_token_ms,
                        "total_latency_ms": rec.total_latency_ms,
                        "backend": rec.backend,
                        "error_category": rec.error_category,
                    }) + "\n")
            except OSError:
                pass  # Non-fatal — silent persist failure

    def add_event(self, event: str) -> None:
        with self._lock:
            ts = datetime.now(timezone.utc).isoformat()
            self._events.append(f"[{ts}] {event}")

    def get_lane_metrics(self, lane: str) -> list[MetricRecord]:
        with self._lock:
            return list(self._brainstem)

    def get_all_metrics(self) -> dict[str, list[MetricRecord]]:
        with self._lock:
            return {
                "brainstem": list(self._brainstem),
            }

    def get_events(self) -> list[str]:
        with self._lock:
            return list(self._events)

    def clear(self) -> None:
        with self._lock:
            self._brainstem.clear()
            self._events.clear()

    def get_summary(self, lane: str) -> dict[str, Any]:
        records = self.get_lane_metrics(lane)
        if not records:
            return {
                "total_requests": 0,
                "success_count": 0,
                "failure_count": 0,
                "avg_generation_tokens_per_second": 0.0,
                "avg_prompt_tokens_per_second": 0.0,
                "avg_total_latency_ms": 0.0,
                "quickest_generation_tokens_per_second": 0.0,
                "slowest_generation_tokens_per_second": 0.0,
                "last_request_time": None,
                "last_success": True,
                "success_rate": 100.0,
                # Failure diagnostic — populated when a failure record exists.
                "last_failure_code": None,
                "last_failure_at": None,
            }
        successes = [r for r in records if r.success]
        failures = [r for r in records if not r.success]
        failure_records = [r for r in failures if r.error_category]
        last_failure = failure_records[-1] if failure_records else None
        gen_tps = [r.generation_tokens_per_second for r in successes if r.generation_tokens_per_second > 0]
        prompt_tps = [r.prompt_tokens_per_second for r in successes if r.prompt_tokens_per_second > 0]
        latencies = [r.total_latency_ms for r in successes]
        total = len(records)
        last_success_record = successes[-1] if successes else None
        return {
            "total_requests": total,
            "success_count": len(successes),
            "failure_count": len(failures),
            "success_rate": round(len(successes) / total * 100, 1) if total > 0 else 100.0,
            "avg_generation_tokens_per_second": round(sum(gen_tps) / len(gen_tps), 2) if gen_tps else 0.0,
            "avg_prompt_tokens_per_second": round(sum(prompt_tps) / len(prompt_tps), 2) if prompt_tps else 0.0,
            "avg_total_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
            "quickest_generation_tokens_per_second": round(max(gen_tps), 2) if gen_tps else 0.0,
            "slowest_generation_tokens_per_second": round(min(gen_tps), 2) if gen_tps else 0.0,
            "last_request_time": records[-1].timestamp if records else None,
            "last_success": records[-1].success if records else True,
            # Cumulative token totals (spec §9)
            "total_prompt_tokens": sum(r.prompt_tokens for r in successes),
            "total_completion_tokens": sum(r.completion_tokens for r in successes),
            "last_prompt_tokens": last_success_record.prompt_tokens if last_success_record else 0,
            "last_completion_tokens": last_success_record.completion_tokens if last_success_record else 0,
            "latest_generation_tokens_per_second": (
                round(last_success_record.generation_tokens_per_second, 2)
                if last_success_record and last_success_record.generation_tokens_per_second > 0
                else 0.0
            ),
            # Server-side timing from llama-server pipeline (spec §9)
            "last_time_to_first_token_ms": (
                round(last_success_record.time_to_first_token_ms, 1)
                if last_success_record and last_success_record.time_to_first_token_ms is not None
                else None
            ),
            "avg_time_to_first_token_ms": (
                round(sum(
                    r.time_to_first_token_ms
                    for r in successes
                    if r.time_to_first_token_ms is not None
                ) / max(len([
                    r for r in successes
                    if r.time_to_first_token_ms is not None
                ]), 1), 1)
                if successes
                else None
            ),
            # Startup / inference failure diagnostic drives the dashboard
            # banner. Raw error text NEVER lives in the store; the dashboard
            # looks up a sanitized public message from the error code.
            "last_failure_code": last_failure.error_category if last_failure else None,
            "last_failure_at": last_failure.timestamp if last_failure else None,
        }


# Module-level singleton — same single instance shared by app.py and
# run_metrics.py. (Avoids creating parallel metric collections.)
# Persists to ``./logs/metrics.jsonl`` when the ``logs/`` directory exists.
_DEFAULT_PERSIST_PATH = CONFIG.metrics_persist_path
METRICS = MetricsStore(persist_path=_DEFAULT_PERSIST_PATH)
