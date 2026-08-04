import unittest
from datetime import date
from unittest.mock import Mock, patch

from etf_data import (
    _fetch_tw_official_bulk,
    _fetch_tw_official_month_quote,
    _fetch_us_nasdaq_quote,
    _parse_tw_market_date,
    _parse_tw_mis_quote,
    _resolved_payout_freq,
)


class OfficialQuoteBulkTest(unittest.TestCase):
    def test_mis_uses_last_trade_instead_of_previous_close_after_hours(self):
        result = _parse_tw_mis_quote({
            "d": "20260804", "z": "-", "pz": "100.65", "y": "102.00",
            "h": "102.25", "l": "99.75", "v": "114397",
        })

        self.assertEqual(result["current_price"], 100.65)
        self.assertEqual(result["price_change"], -1.35)
        self.assertAlmostEqual(result["price_change_percent"], -1.3235)
        self.assertEqual(result["volume"], 114_397_000)
        self.assertTrue(result["is_after_hours"])

    def test_mis_rejects_previous_close_when_today_has_activity(self):
        result = _parse_tw_mis_quote({
            "d": "20260804", "z": "-", "pz": "-", "y": "102.00",
            "h": "102.25", "l": "99.75", "v": "114397",
        })

        self.assertIsNone(result)

    def test_mis_accepts_true_unchanged_close(self):
        result = _parse_tw_mis_quote({
            "d": "20260804", "z": "32.57", "pz": "32.57", "y": "32.57",
            "h": "32.88", "l": "32.32", "v": "56047",
        })

        self.assertEqual(result["current_price"], 32.57)
        self.assertEqual(result["price_change_percent"], 0.0)

    def test_confirmed_no_dividend_can_clear_wrong_frequency(self):
        self.assertEqual(
            _resolved_payout_freq("不配息", "季配", confirmed=True),
            "不配息",
        )

    def test_unconfirmed_no_dividend_preserves_known_frequency(self):
        self.assertEqual(
            _resolved_payout_freq("不配息", "季配", confirmed=False),
            "季配",
        )

    def test_parses_roc_market_date(self):
        self.assertEqual(_parse_tw_market_date("1150727"), date(2026, 7, 27))

    @patch("etf_data.req_lib.get")
    def test_merges_twse_and_tpex_quotes(self, get):
        twse = Mock()
        twse.raise_for_status.return_value = None
        twse.json.return_value = [{
            "Date": "1150724",
            "Code": "0050",
            "ClosingPrice": "101.50",
            "Change": "-0.20",
            "HighestPrice": "102.00",
            "LowestPrice": "101.20",
            "TradeVolume": "12345678",
        }]
        tpex = Mock()
        tpex.raise_for_status.return_value = None
        tpex.json.return_value = [{
            "Date": "1150727",
            "SecuritiesCompanyCode": "00679B",
            "Close": "26.89",
            "Change": "+0.24",
            "High": "26.90",
            "Low": "26.82",
            "TradingShares": "18450000",
        }]
        get.side_effect = [twse, tpex]

        result = _fetch_tw_official_bulk()

        self.assertEqual(set(result), {"0050", "00679B"})
        self.assertEqual(result["0050"]["volume"], 12_345_678)
        self.assertEqual(result["0050"]["quote_date"], date(2026, 7, 24))
        self.assertAlmostEqual(result["00679B"]["price_change_percent"], 0.9006)

    @patch("etf_data.req_lib.get")
    def test_reads_special_etf_from_monthly_official_source(self, get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": [
                ["115/07/24", "100", "0", "32.00", "32.10", "31.90", "32.00"],
                ["115/07/27", "164,051", "0", "32.29", "32.30", "32.20", "32.20"],
            ]
        }
        get.return_value = response

        result = _fetch_tw_official_month_quote("00775B")

        self.assertEqual(result["quote_date"], date(2026, 7, 27))
        self.assertEqual(result["volume"], 164_051)
        self.assertEqual(result["current_price"], 32.2)
        self.assertAlmostEqual(result["price_change_percent"], 0.625)

    @patch("etf_data.req_lib.get")
    def test_reads_us_quote_from_nasdaq(self, get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "tradesTable": {
                    "rows": [
                        {
                            "date": "07/24/2026", "close": "$738.93",
                            "volume": "44,781,980", "high": "$743.72", "low": "$737.29",
                        },
                        {
                            "date": "07/23/2026", "close": "$738.10",
                            "volume": "40,000,000", "high": "$740.00", "low": "$735.00",
                        },
                    ]
                }
            }
        }
        get.return_value = response

        result = _fetch_us_nasdaq_quote("SPY")

        self.assertEqual(result["quote_date"], date(2026, 7, 24))
        self.assertEqual(result["volume"], 44_781_980)
        self.assertEqual(result["current_price"], 738.93)
        self.assertAlmostEqual(result["price_change"], 0.83)


if __name__ == "__main__":
    unittest.main()
