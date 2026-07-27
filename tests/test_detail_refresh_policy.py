import unittest
from datetime import date

from routes.etf_routes import _missing_returns_need_refresh


class DetailRefreshPolicyTest(unittest.TestCase):
    def test_new_etf_does_not_retry_unavailable_returns(self):
        row = {
            "listing_date": "2026-05-12",
            "annual_return_1y": None,
            "annual_return_3y": None,
        }
        self.assertFalse(
            _missing_returns_need_refresh(row, today=date(2026, 7, 28))
        )

    def test_established_etf_retries_missing_returns(self):
        row = {
            "listing_date": "2020-01-02",
            "annual_return_1y": None,
            "annual_return_3y": None,
        }
        self.assertTrue(
            _missing_returns_need_refresh(row, today=date(2026, 7, 28))
        )

    def test_any_available_return_avoids_redundant_retry(self):
        row = {
            "listing_date": "2020-01-02",
            "annual_return_1y": 12.5,
            "annual_return_3y": None,
        }
        self.assertFalse(
            _missing_returns_need_refresh(row, today=date(2026, 7, 28))
        )


if __name__ == "__main__":
    unittest.main()
