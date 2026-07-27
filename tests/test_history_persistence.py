import unittest

from routes.etf_routes import _history_points_for_storage


class HistoryPersistenceTest(unittest.TestCase):
    def test_keeps_only_valid_iso_dated_positive_prices(self):
        result = _history_points_for_storage({
            "labels": ["2026-07-24", "07/25 09:30", "bad", "2026-07-27"],
            "prices": [101.5, 102.0, 103.0, 0],
        })
        self.assertEqual(
            result,
            [{"date": "2026-07-24", "close": 101.5}],
        )

    def test_mismatched_series_uses_only_complete_pairs(self):
        result = _history_points_for_storage({
            "labels": ["2026-07-24", "2026-07-25"],
            "prices": [50],
        })
        self.assertEqual(result, [{"date": "2026-07-24", "close": 50.0}])


if __name__ == "__main__":
    unittest.main()
