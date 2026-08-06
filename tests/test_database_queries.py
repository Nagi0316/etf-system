import unittest

from database_queries import latest_daily_join


class LatestDailyJoinTest(unittest.TestCase):
    def test_builds_latest_positive_price_join(self):
        sql = latest_daily_join("m")

        self.assertIn("LEFT JOIN", sql)
        self.assertIn("WHERE current_price > 0", sql)
        self.assertIn("MAX(date) AS max_date", sql)
        self.assertIn("m.ticker = d.ticker", sql)

    def test_supports_required_inner_join(self):
        sql = latest_daily_join("m", "JOIN")
        self.assertIn("\nJOIN (", sql)

    def test_rejects_unsafe_alias_and_join_type(self):
        with self.assertRaises(ValueError):
            latest_daily_join("m; DROP TABLE etf_master")
        with self.assertRaises(ValueError):
            latest_daily_join("m", "RIGHT JOIN")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
