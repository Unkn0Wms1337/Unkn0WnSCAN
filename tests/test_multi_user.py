from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from avito import AvitoBlockedError
from monitor import Monitor
from storage import Storage


class MultiUserStorageTests(unittest.TestCase):
    def test_subscriptions_and_seller_blocks_are_chat_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "bot.sqlite3")
            try:
                first, created_first = storage.add_subscription(
                    101, "https://www.avito.ru/search?q=phone", "phone"
                )
                second, created_second = storage.add_subscription(
                    202, "https://www.avito.ru/search?q=phone", "phone"
                )
                self.assertTrue(created_first)
                self.assertTrue(created_second)
                self.assertNotEqual(first.id, second.id)
                self.assertEqual([s.id for s in storage.list_subscriptions(101)], [first.id])
                self.assertEqual([s.id for s in storage.list_subscriptions(202)], [second.id])

                storage.block_seller(101, "seller-1")
                self.assertEqual(storage.blocked_sellers(101), {"seller-1"})
                self.assertEqual(storage.blocked_sellers(202), set())
                self.assertFalse(storage.delete_subscription(first.id, 202))
                self.assertIsNotNone(storage.get_subscription(first.id))
            finally:
                storage.close()


class SearchRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_after_avito_ip_block(self) -> None:
        avito = AsyncMock()
        avito.fetch_search.side_effect = [AvitoBlockedError("blocked"), []]
        monitor = Monitor(
            client=AsyncMock(),
            storage=AsyncMock(),
            avito_client=avito,
            interval=30,
            initial_pages=1,
            check_pages=1,
            max_new_per_check=10,
            fetch_details=False,
        )
        with unittest.mock.patch("monitor.asyncio.sleep", new=AsyncMock()):
            result = await monitor._fetch_search_resilient("https://avito.test", 1)
        self.assertEqual(result, [])
        self.assertEqual(avito.fetch_search.await_count, 2)


if __name__ == "__main__":
    unittest.main()
