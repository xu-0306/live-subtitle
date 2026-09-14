from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


@dataclass
class SchedulerMetrics:
    submitted: int = 0
    completed: int = 0
    cancelled: int = 0
    failed: int = 0
    started: int = 0
    run_finished: int = 0
    active: int = 0
    queued: int = 0
    total_queue_ms: float = 0.0
    total_run_ms: float = 0.0

    def snapshot(self) -> dict[str, object]:
        average_queue_ms = self.total_queue_ms / self.started if self.started else 0.0
        average_run_ms = self.total_run_ms / self.run_finished if self.run_finished else 0.0
        return {
            "submitted": self.submitted,
            "completed": self.completed,
            "cancelled": self.cancelled,
            "failed": self.failed,
            "started": self.started,
            "active": self.active,
            "queued": self.queued,
            "average_queue_ms": round(average_queue_ms, 2),
            "average_run_ms": round(average_run_ms, 2),
        }


class TranslationScheduler:
    """Bound concurrent translations globally and per connection profile.

    Latest-wins ownership remains in the WebSocket session: only HTTP-backed
    providers are cancelled in-flight, while local thread-backed inference is
    allowed to finish and its stale result is discarded safely.
    """

    def __init__(
        self,
        global_max_concurrency: object = 8,
        default_profile_max_concurrency: object = 4,
    ) -> None:
        self.global_max_concurrency = _positive_int(global_max_concurrency, 8)
        self.default_profile_max_concurrency = _positive_int(
            default_profile_max_concurrency, 4
        )
        self._global_semaphore = asyncio.Semaphore(self.global_max_concurrency)
        self._profile_semaphores: dict[str, asyncio.Semaphore] = {}
        self._profile_limits: dict[str, int] = {}
        self.metrics = SchedulerMetrics()

    def _profile_semaphore(
        self, profile_id: str, limit: Optional[int]
    ) -> asyncio.Semaphore:
        normalized_limit = _positive_int(
            limit, self.default_profile_max_concurrency
        )
        key = profile_id or "default"
        semaphore = self._profile_semaphores.get(key)
        if semaphore is None:
            # A profile id is one scheduling domain. Its first configured limit
            # remains stable for the scheduler lifetime so concurrent callers
            # cannot bypass a limit by presenting a different value later.
            semaphore = asyncio.Semaphore(normalized_limit)
            self._profile_semaphores[key] = semaphore
            self._profile_limits[key] = normalized_limit
        return semaphore

    async def translate(
        self,
        provider,
        text: str,
        source_lang: Optional[str],
        target_lang: Optional[str] = None,
    ) -> str:
        profile_id = str(getattr(provider, "profile_id", "default") or "default")
        profile_limit = getattr(provider, "max_concurrency", None)
        profile_semaphore = self._profile_semaphore(profile_id, profile_limit)
        queued_at = time.monotonic()
        started_at: Optional[float] = None
        cancellation_recorded = False
        self.metrics.submitted += 1
        self.metrics.queued += 1
        try:
            async with profile_semaphore:
                async with self._global_semaphore:
                    started_at = time.monotonic()
                    self.metrics.queued -= 1
                    self.metrics.active += 1
                    self.metrics.started += 1
                    self.metrics.total_queue_ms += (started_at - queued_at) * 1000.0
                    try:
                        result = await provider.atranslate(
                            text, source_lang, target_lang
                        )
                    except asyncio.CancelledError:
                        self.metrics.cancelled += 1
                        cancellation_recorded = True
                        raise
                    except Exception:
                        self.metrics.failed += 1
                        raise
                    finally:
                        self.metrics.active -= 1
                        self.metrics.total_run_ms += (
                            time.monotonic() - started_at
                        ) * 1000.0
                        self.metrics.run_finished += 1
                    self.metrics.completed += 1
                    return result
        except asyncio.CancelledError:
            # Cancellation can happen while waiting for either semaphore.
            if started_at is None:
                self.metrics.queued -= 1
            if not cancellation_recorded:
                self.metrics.cancelled += 1
            raise
