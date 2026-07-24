import unittest
from contextlib import contextmanager
from unittest.mock import patch

from routes.etf_routes import _fetch_db_price_history


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.query = ""
        self.params = ()

    def execute(self, query, params):
        self.query = query
        self.params = params

    def fetchall(self):
        return self.rows


class PriceHistoryDatabaseFallbackTest(unittest.TestCase):
    def test_intraday_period_uses_recent_closes_without_claiming_intraday(self):
        cursor = _Cursor([
            {"date": "2026-07-24", "current_price": 104.8},
            {"date": "2026-07-23", "current_price": 103.2},
        ])

        @contextmanager
        def fake_db():
            yield object(), cursor

        with patch("routes.etf_routes.get_db", fake_db):
            result = _fetch_db_price_history("0050", "1D")

        self.assertEqual(result["labels"], ["2026-07-23", "2026-07-24"])
        self.assertEqual(result["prices"], [103.2, 104.8])
        self.assertFalse(result["is_intraday"])
        self.assertEqual(result["fallback_reason"], "recent_closes")
        self.assertIn("ORDER BY date DESC", cursor.query)

    def test_partial_history_with_two_points_is_still_drawable(self):
        cursor = _Cursor([
            {"date": "2026-07-23", "current_price": 103.2},
            {"date": "2026-07-24", "current_price": 104.8},
        ])

        @contextmanager
        def fake_db():
            yield object(), cursor

        with patch("routes.etf_routes.get_db", fake_db):
            result = _fetch_db_price_history("0050", "1Y")

        self.assertEqual(len(result["prices"]), 2)
        self.assertTrue(result["is_partial"])

    def test_new_etf_with_one_close_still_returns_chart_data(self):
        cursor = _Cursor([
            {"date": "2026-07-24", "current_price": 15.8},
        ])

        @contextmanager
        def fake_db():
            yield object(), cursor

        with patch("routes.etf_routes.get_db", fake_db):
            result = _fetch_db_price_history("00999", "1M")

        self.assertEqual(result["labels"], ["2026-07-24"])
        self.assertEqual(result["prices"], [15.8])


if __name__ == "__main__":
    unittest.main()
