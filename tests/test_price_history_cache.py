import asyncio
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from cache import cache
from routes.etf_routes import get_price_history


class _MarketCursor:
    def execute(self, _query, _params):
        return None

    def fetchone(self):
        return {"market": "TW"}


@contextmanager
def _fake_db():
    yield object(), _MarketCursor()


class PriceHistoryCacheTest(unittest.TestCase):
    def setUp(self):
        cache.delete_prefix("hist:0050:")

    def tearDown(self):
        cache.delete_prefix("hist:0050:")

    def test_cache_hit_returns_a_fresh_json_response_with_body(self):
        history = {
            "labels": ["2026-07-16", "2026-07-17"],
            "prices": [105.25, 106.4],
            "is_intraday": False,
            "is_partial": False,
        }

        with (
            patch("routes.etf_routes.get_db", _fake_db),
            patch("routes.etf_routes._fetch_db_price_history", return_value=history),
        ):
            first = asyncio.run(get_price_history("0050", "1Y"))
            second = asyncio.run(get_price_history("0050", "1Y"))

        self.assertGreater(len(first.body), 0)
        self.assertEqual(first.body, second.body)
        self.assertEqual(json.loads(second.body)["prices"], history["prices"])
        self.assertIsInstance(cache.get("hist:0050:1Y:raw"), dict)


if __name__ == "__main__":
    unittest.main()
