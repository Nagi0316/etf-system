import unittest

from services.price_adjustment import adjust_detected_splits


class PriceAdjustmentTest(unittest.TestCase):
    def test_forward_split_back_adjusts_prior_prices(self):
        prices, events = adjust_detected_splits(
            ["2025-06-09", "2025-06-10", "2025-06-18", "2025-06-19"],
            [184.0, 188.0, 47.0, 48.0],
        )

        self.assertEqual(prices, [46.0, 47.0, 47.0, 48.0])
        self.assertEqual(events[0]["ratio"], 4.0)
        self.assertEqual(events[0]["date"], "2025-06-18")

    def test_reverse_split_back_adjusts_prior_prices(self):
        prices, events = adjust_detected_splits(
            ["2025-01-02", "2025-01-03", "2025-01-06"],
            [9.5, 10.0, 100.0],
        )

        self.assertEqual(prices, [95.0, 100.0, 100.0])
        self.assertEqual(events[0]["ratio"], 0.1)

    def test_large_but_non_split_move_is_not_rewritten(self):
        raw = [100.0, 60.0, 62.0]
        prices, events = adjust_detected_splits(
            ["2025-01-02", "2025-01-03", "2025-01-06"], raw
        )

        self.assertEqual(prices, raw)
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
