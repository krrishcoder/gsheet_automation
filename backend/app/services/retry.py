"""Exponential backoff with full jitter for transient external-API failures."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 30.0) -> float:
    """Delay before retry number ``attempt`` (1-based): random in [0, min(cap, base * 2**(attempt-1))]."""
    return random.uniform(0, min(cap, base * (2 ** (attempt - 1))))


def call_with_retries(
    func: Callable[[], T],
    *,
    is_retryable: Callable[[BaseException], bool],
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    retry_after: Callable[[BaseException], float | None] = lambda _exc: None,
    description: str = "call",
) -> T:
    attempt = 0
    while True:
        attempt += 1
        try:
            return func()
        except Exception as exc:
            if attempt >= max_attempts or not is_retryable(exc):
                raise
            delay = retry_after(exc)
            if delay is None:
                delay = backoff_delay(attempt, base=base_delay, cap=max_delay)
            log.warning(
                "transient failure, retrying",
                extra={"operation": description, "attempt": attempt, "delay_s": round(delay, 2), "error": str(exc)},
            )
            sleep(delay)
