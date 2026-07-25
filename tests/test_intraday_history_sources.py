import unittest
from unittest.mock import patch

import pandas as pd

from routes.etf_routes import (
    _fetch_twse_intraday_history,
    _frame_to_price_history,
    _yahoo_chart_urls,
)


class _TwseResponse:
    status_code = 200
    content = b"{}"

    def json(self):
        return {
            "rtcode": "0000",
            "ohlcArray": [
                {"c": "101.25", "t": "1784854860000"},
                {"c": "102.50", "t": "1784854920000"},
            ],
        }


class IntradayHistorySourcesTest(unittest.TestCase):
    def test_builds_both_yahoo_chart_hosts(self):
        urls = _yahoo_chart_urls("0050.TW", "5d", "15m")

        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].startswith("https://query2.finance.yahoo.com/"))
        self.assertTrue(urls[1].startswith("https://query1.finance.yahoo.com/"))
        self.assertTrue(all("range=5d" in url and "interval=15m" in url for url in urls))

    def test_normalizes_yfinance_intraday_frame(self):
        frame = pd.DataFrame(
            {"Close": [101.25, 102.5]},
            index=pd.to_datetime([
                "2026-07-24 09:00:00+08:00",
                "2026-07-24 09:15:00+08:00",
            ]),
        )

        result = _frame_to_price_history(frame, "5D", "Asia/Taipei")

        self.assertEqual(result["labels"], ["07/24 09:00", "07/24 09:15"])
        self.assertEqual(result["prices"], [101.25, 102.5])
        self.assertTrue(result["is_intraday"])

    @patch("routes.etf_routes._req.get", return_value=_TwseResponse())
    def test_fetches_twse_mis_intraday_points_directly(self, request_get):
        result = _fetch_twse_intraday_history("0050")

        self.assertEqual(result["prices"], [101.25, 102.5])
        self.assertTrue(result["is_intraday"])
        self.assertEqual(result["source"], "twse_mis")
        self.assertEqual(request_get.call_args.kwargs["params"]["ch"], "0050.tw")


if __name__ == "__main__":
    unittest.main()
