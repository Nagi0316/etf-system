import asyncio
import json
import unittest
from unittest.mock import patch

import main


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


if __name__ == "__main__":
    unittest.main()
