import asyncio
import unittest
from unittest.mock import patch

from routes import etf_routes


class OnDemandRateLimitTest(unittest.TestCase):
    def test_each_background_fetch_counts_rate_limit_once(self):
        with (
            patch.object(etf_routes, "_check_demand_rate", return_value=True) as limiter,
            patch.object(etf_routes, "fetch_one_etf") as fetch,
        ):
            result = asyncio.run(
                etf_routes._on_demand_fetch("SPY", "203.0.113.10")
            )

        self.assertIsNone(result)
        limiter.assert_called_once_with("203.0.113.10")
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
