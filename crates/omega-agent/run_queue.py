"""RunQueue — inference queue with timeout and depth tracking.

Replaces the bare ``threading.Lock()`` that serialized all inference across
both lanes. The queue wraps the lock with:

  * Configurable timeout per request (fail fast instead of hanging).
  * Queue-depth tracking for monitoring.
  * A clean context-manager interface.

Why a module instead of a bare lock in app.py:

  * **Locality:** concurrency policy lives in one place, not as a naked
    primitive in the orchestrator.
  * **Resilience:** a timeout prevents a hung request from locking the
    system forever.
  * **Observability:** ``depth()`` and ``max_depth()`` feed into
    PublicSnapshot.
  * **Testability:** the queue can be unit-tested without ZeroGPU.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Generator

from domain import Lane


class RunQueueTimeout(Exception):
    """Raised when acquiring the inference queue times out."""

    def __init__(self, lane: Lane, timeout_s: float) -> None:
        self.lane = lane
        self.timeout_s = timeout_s
        super().__init__(
            f"inference queue acquire timed out after {timeout_s}s for lane {lane.value}"
        )


class RunQueue:
    """Thread-safe inference queue with timeout and depth tracking.

    Usage::

        with run_queue.acquire(lane):
            result = execute_lane(lane.value, body)
    """

    def __init__(self, timeout_s: float = 300.0) -> None:
        self._lock = threading.Lock()
        self._timeout_s = timeout_s
        self._depth = 0
        self._max_depth = 0

    # ── Public ────────────────────────────────────────────────────────

    @contextmanager
    def acquire(self, lane: Lane) -> Generator[None, None, None]:
        """Acquire the queue with a timeout.

        Raises :class:`RunQueueTimeout` if the lock cannot be acquired
        within the configured timeout.
        """
        acquired = self._lock.acquire(timeout=self._timeout_s)
        if not acquired:
            raise RunQueueTimeout(lane, self._timeout_s)
        self._depth += 1
        self._max_depth = max(self._max_depth, self._depth)
        try:
            yield
        finally:
            self._depth -= 1
            self._lock.release()

    @property
    def depth(self) -> int:
        """Current queue depth (0 = idle, 1 = running, >1 = queued)."""
        return self._depth

    @property
    def max_depth(self) -> int:
        """Peak queue depth since construction."""
        return self._max_depth

    def reset_metrics(self) -> None:
        """Reset peak-depth tracking (for dashboard refresh)."""
        self._max_depth = self._depth
