"""Thread-safe in-memory metrics store (extracted from app.py).

Small, dependency-light module. Holds a rolling deque of :class:`MetricRecord`
per lane and a parallel event log. Public consumers ask for summaries; this
module never writes to disk.
"""

from __future__ import annotations

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
    backend: str = "cuda"
    gpu_offload_verified: bool = True
    finish_reason: str = "stop"
    error_category: str | None = None


class MetricsStore:
    """Thread-safe in-memory rolling metrics store (no disk writes)."""

    def __init__(self, maxlen: int = 500, event_maxlen: int = 200) -> None:
        self._maxlen = maxlen
        self._microbrain: deque[MetricRecord] = deque(maxlen=maxlen)
        self._mainbrain: deque[MetricRecord] = deque(maxlen=maxlen)
        self._events: deque[str] = deque(maxlen=event_maxlen)
        self._lock = Lock()

    def record(self, rec: MetricRecord) -> None:
        with self._lock:
            if rec.lane == "microbrain":
                self._microbrain.append(rec)
            else:
                self._mainbrain.append(rec)

    def add_event(self, event: str) -> None:
        with self._lock:
            ts = datetime.now(timezone.utc).isoformat()
            self._events.append(f"[{ts}] {event}")

    def get_lane_metrics(self, lane: str) -> list[MetricRecord]:
        with self._lock:
            if lane == "microbrain":
                return list(self._microbrain)
            return list(self._mainbrain)

    def get_all_metrics(self) -> dict[str, list[MetricRecord]]:
        with self._lock:
            return {
                "microbrain": list(self._microbrain),
                "mainbrain": list(self._mainbrain),
            }

    def get_events(self) -> list[str]:
        with self._lock:
            return list(self._events)

    def clear(self) -> None:
        with self._lock:
            self._microbrain.clear()
            self._mainbrain.clear()
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
            }
        successes = [r for r in records if r.success]
        failures = [r for r in records if not r.success]
        gen_tps = [r.generation_tokens_per_second for r in successes if r.generation_tokens_per_second > 0]
        prompt_tps = [r.prompt_tokens_per_second for r in successes if r.prompt_tokens_per_second > 0]
        latencies = [r.total_latency_ms for r in successes]
        total = len(records)
        last_success = successes[-1] if successes else None
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
            "last_prompt_tokens": last_success.prompt_tokens if last_success else 0,
            "last_completion_tokens": last_success.completion_tokens if last_success else 0,
            "latest_generation_tokens_per_second": (
                round(last_success.generation_tokens_per_second, 2)
                if last_success and last_success.generation_tokens_per_second > 0
                else 0.0
            ),
        }


# Module-level singleton — same single instance shared by app.py and
# run_metrics.py. (Avoids creating parallel metric collections.)
METRICS = MetricsStore()
