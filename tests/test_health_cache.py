import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
import main
from cache import cache


class HealthCacheTest(unittest.TestCase):
    def test_data_health_cache_returns_fresh_response_body(self):
        cached = {
            "status": "ok",
            "summary": {"missing_etfs": 0},
            "issues": [],
        }
        with patch("cache.cache.get", return_value=cached):
            first = asyncio.run(main.health_data())
            second = asyncio.run(main.health_data())

        self.assertEqual(json.loads(first.body), cached)
        self.assertEqual(json.loads(second.body), cached)
        self.assertIsNot(first, second)

    def test_detail_health_cache_returns_fresh_response_body(self):
        cached = {"status": "ok", "summary": {"fresh": 89}, "etfs": []}
        with patch("cache.cache.get", return_value=cached):
            first = asyncio.run(main.health_detail())
            second = asyncio.run(main.health_detail())

        self.assertEqual(json.loads(first.body), cached)
        self.assertEqual(json.loads(second.body), cached)
        self.assertIsNot(first, second)


class HealthSqliteCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(database, "USE_MYSQL", False),
            patch.object(
                database,
                "SQLITE_PATH",
                str(Path(self.tmp.name) / "health.db"),
            ),
        ]
        for item in self.patches:
            item.start()
        database.init_db()
        cache.delete("health:data")

    def tearDown(self):
        cache.delete("health:data")
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_full_data_health_audit_runs_on_sqlite(self):
        response = asyncio.run(main.health_data())
        body = json.loads(response.body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertIn("dividend_quality_by_market", body["summary"])
        self.assertIn("dividend_sync", body["summary"])


if __name__ == "__main__":
    unittest.main()
