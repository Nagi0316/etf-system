import unittest

from routes.etf_routes import _merge_price_histories


class PriceHistoryMergeTest(unittest.TestCase):
    def test_merges_long_history_with_newer_database_rows(self):
        official = {
            "labels": ["2025-06-17", "2025-06-18", "2025-06-19"],
            "prices": [188.0, 47.0, 47.5],
        }
        database = {
            "labels": ["2025-06-19", "2026-07-17"],
            "prices": [47.6, 100.15],
            "is_partial": True,
        }

        merged = _merge_price_histories("5Y", official, database)

        self.assertEqual(
            merged["labels"],
            ["2025-06-17", "2025-06-18", "2025-06-19", "2026-07-17"],
        )
        self.assertEqual(merged["prices"], [188.0, 47.0, 47.6, 100.15])
        self.assertTrue(merged["is_partial"])

    def test_ignores_invalid_points(self):
        merged = _merge_price_histories(
            "1M",
            {"labels": ["2026-07-01", "2026-07-02", "2026-07-03"],
             "prices": [10, None, -1]},
            {"labels": ["2026-07-04"], "prices": [11]},
        )

        self.assertEqual(merged["labels"], ["2026-07-01", "2026-07-04"])
        self.assertEqual(merged["prices"], [10.0, 11.0])


if __name__ == "__main__":
    unittest.main()
