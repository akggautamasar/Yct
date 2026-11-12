"""Offline checks for persistent catalog behavior; no YCT requests made."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import catalog


class CatalogCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.original_path = catalog.DB_PATH
        catalog.DB_PATH = os.path.join(self.work.name, "test-catalog.sqlite3")
        catalog._initialize()

    def tearDown(self):
        catalog.DB_PATH = self.original_path
        self.work.cleanup()

    async def test_persistence_and_new_arrivals(self):
        catalog._upsert_categories([{"id": 11, "name": "Engineering"}])
        first = [{"detail_id": 1, "book_id": 489, "name": "Engineering Drawing", "cover": ""}]
        self.assertEqual(catalog._save_books(11, first), 0)
        self.assertEqual(len(catalog.books(11)), 1)
        self.assertFalse(catalog.books(11)[0]["is_new"])
        second = first + [{"detail_id": 2, "book_id": 490, "name": "New Book", "cover": ""}]
        self.assertEqual(catalog._save_books(11, second), 1)
        self.assertTrue(next(x for x in catalog.books(11) if x["book_id"] == 490)["is_new"])
        self.assertEqual(catalog._save_books(11, second), 0)
        self.assertEqual(catalog.status()["books"], 2)
        self.assertEqual(catalog.status()["new_books"], 1)

    async def test_page_count_persists(self):
        catalog._upsert_categories([{"id": 11, "name": "Engineering"}])
        catalog._save_books(11, [{"detail_id": 1, "book_id": 489, "name": "Book", "cover": ""}])
        catalog.remember_page_count(489, 288)
        self.assertEqual(catalog.cached_page_count(489), 288)
        self.assertEqual(catalog.books(11)[0]["total_pages"], 288)
        catalog._initialize()  # reopen same database; metadata survives
        self.assertEqual(catalog.cached_page_count(489), 288)

    async def test_incremental_sync_does_not_resolve_existing_details(self):
        catalog._upsert_categories([{"id": 11, "name": "Engineering"}])
        catalog._save_books(11, [{"detail_id": 1, "book_id": 489, "name": "Book", "cover": ""}])
        with patch.object(catalog.scraper, "get_books_in_category", new=AsyncMock(
            return_value=[{"detail_id": 1}, {"detail_id": 2}]
        )), patch.object(catalog.scraper, "get_book_details", new=AsyncMock(
            return_value=("Fresh Book", 490)
        )) as details:
            self.assertEqual(await catalog.sync_category(11), 1)
            self.assertEqual(details.await_count, 1)
        self.assertEqual(len(catalog.books(11)), 2)


if __name__ == "__main__":
    unittest.main()
