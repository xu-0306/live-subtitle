from __future__ import annotations

import asyncio
import unittest
from collections import defaultdict

from backend.translation_scheduler import TranslationScheduler


class ConcurrencyProbe:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.by_profile: dict[str, int] = defaultdict(int)
        self.max_by_profile: dict[str, int] = defaultdict(int)

    async def run(self, profile_id: str, text: str) -> str:
        self.active += 1
        self.by_profile[profile_id] += 1
        self.max_active = max(self.max_active, self.active)
        self.max_by_profile[profile_id] = max(
            self.max_by_profile[profile_id], self.by_profile[profile_id]
        )
        try:
            await asyncio.sleep(0.02)
            return text
        finally:
            self.active -= 1
            self.by_profile[profile_id] -= 1


class FakeProvider:
    cancellation_safe = True

    def __init__(
        self, profile_id: str, max_concurrency: int, probe: ConcurrencyProbe
    ) -> None:
        self.profile_id = profile_id
        self.max_concurrency = max_concurrency
        self.probe = probe

    async def atranslate(self, text: str, source_lang, target_lang=None) -> str:
        return await self.probe.run(self.profile_id, text)


class TranslationSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_enforces_global_and_per_profile_limits(self) -> None:
        scheduler = TranslationScheduler(
            global_max_concurrency=2,
            default_profile_max_concurrency=1,
        )
        probe = ConcurrencyProbe()
        profile_a = FakeProvider("a", 1, probe)
        profile_b = FakeProvider("b", 2, probe)
        await asyncio.gather(
            *[
                scheduler.translate(provider, str(index), "en")
                for index, provider in enumerate(
                    [profile_a, profile_a, profile_b, profile_b]
                )
            ]
        )
        self.assertLessEqual(probe.max_active, 2)
        self.assertLessEqual(probe.max_by_profile["a"], 1)
        self.assertLessEqual(probe.max_by_profile["b"], 2)
        self.assertEqual(scheduler.metrics.completed, 4)
        self.assertEqual(scheduler.metrics.snapshot()["queued"], 0)

    async def test_profile_id_is_single_domain_when_limits_disagree(self) -> None:
        scheduler = TranslationScheduler(global_max_concurrency=4)
        probe = ConcurrencyProbe()
        first = FakeProvider("same", 1, probe)
        second = FakeProvider("same", 3, probe)
        await asyncio.gather(
            scheduler.translate(first, "one", "en"),
            scheduler.translate(second, "two", "en"),
        )
        self.assertEqual(probe.max_by_profile["same"], 1)


if __name__ == "__main__":
    unittest.main()
