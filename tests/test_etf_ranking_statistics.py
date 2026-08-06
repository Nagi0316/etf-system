import unittest

from routes.etf_routes import _build_etf_index_stats


class EtfIndexStatsTest(unittest.TestCase):
    def test_separates_full_catalog_from_quote_tracked_pool(self):
        rows = [
            {"ticker": "0050", "market": "TW", "is_hot": 1},
            {"ticker": "00878", "market": "TW", "is_hot": 0},
            {"ticker": "SPY", "market": "US", "is_hot": 1},
        ]

        stats = _build_etf_index_stats(rows)

        self.assertEqual(stats["catalog_total"], 3)
        self.assertEqual(stats["tw_active"], 2)
        self.assertEqual(stats["us_curated"], 1)
        self.assertEqual(stats["quote_tracked_total"], 2)
        self.assertEqual(stats["quote_tracked_tw"], 1)
        self.assertEqual(stats["quote_tracked_us"], 1)

    def test_ignores_unknown_market_rows(self):
        stats = _build_etf_index_stats([
            {"ticker": "UNKNOWN", "market": "OTHER", "is_hot": 1},
        ])

        self.assertEqual(stats["catalog_total"], 0)
        self.assertEqual(stats["quote_tracked_total"], 0)


if __name__ == "__main__":
    unittest.main()
