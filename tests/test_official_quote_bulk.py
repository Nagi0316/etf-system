import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo
from unittest.mock import Mock, patch

from etf_market_data import (
    _fetch_tw_official_bulk,
    _fetch_tw_official_month_quote,
    _fetch_us_nasdaq_quote,
    _get_with_retry,
    _parse_tw_market_date,
    _parse_tw_mis_quote,
    _resolved_payout_freq,
    _resolved_dividend_status,
    _expected_tw_quote_day,
    _tw_market_session_open,
    _tw_quote_is_fresh,
)
from routes.etf_routes import _ranking_updated_at


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

    @patch("etf_market_data.time.sleep")
    def test_final_rate_limit_attempt_returns_without_extra_wait(self, sleep):
        session = Mock()
        response = Mock(status_code=429)
        session.get.return_value = response

        result = _get_with_retry(
            session,
            "https://example.invalid/quote",
            max_attempts=1,
        )

        self.assertIs(result, response)
        sleep.assert_not_called()

    def test_dividend_status_distinguishes_missing_from_no_distribution(self):
        self.assertEqual(
            _resolved_dividend_status(None, "不配息", False, "UNKNOWN"),
            "unknown",
        )
        self.assertEqual(
            _resolved_dividend_status(0, "不配息", False, "00631L"),
            "not_applicable",
        )
        self.assertEqual(
            _resolved_dividend_status(3.2, "季配", False, "0056"),
            "estimated",
        )
        self.assertEqual(
            _resolved_dividend_status(3.2, "季配", True, "0056"),
            "confirmed",
        )

    def test_ranking_timestamp_uses_taipei_timezone(self):
        with patch("routes.etf_routes.datetime") as dt:
            dt.now.return_value = datetime(2026, 8, 4, 19, 5)
            self.assertEqual(_ranking_updated_at(), "19:05")
            self.assertEqual(dt.now.call_args.args[0].key, "Asia/Taipei")

    def test_expected_tw_quote_day_rolls_weekend_back(self):
        sunday = datetime(2026, 8, 9, 18, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        self.assertEqual(_expected_tw_quote_day(sunday), date(2026, 8, 7))

    def test_tw_market_session_boundary(self):
        open_time = datetime(2026, 8, 6, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        after_close = datetime(2026, 8, 6, 13, 31, tzinfo=ZoneInfo("Asia/Taipei"))
        self.assertTrue(_tw_market_session_open(open_time))
        self.assertFalse(_tw_market_session_open(after_close))

    def test_rejects_stale_official_quote_after_market_open(self):
        expected = date(2026, 8, 4)
        self.assertFalse(_tw_quote_is_fresh({"quote_date": date(2026, 8, 3)}, expected))
        self.assertTrue(_tw_quote_is_fresh({"quote_date": "2026-08-04"}, expected))

    def test_parses_roc_market_date(self):
        self.assertEqual(_parse_tw_market_date("1150727"), date(2026, 7, 27))

    @patch("etf_market_data.req_lib.get")
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

    @patch("etf_market_data.req_lib.get")
    def test_uses_official_csv_when_twse_openapi_resets(self, get):
        csv_response = Mock()
        csv_response.raise_for_status.return_value = None
        csv_response.json.side_effect = ValueError("CSV response")
        csv_response.content = (
            "日期,證券代號,收盤價,漲跌價差,最高價,最低價,成交股數\n"
            "1150805,0050,103.80,-0.65,104.10,103.20,12345678\n"
        ).encode("utf-8")
        tpex_response = Mock()
        tpex_response.raise_for_status.return_value = None
        tpex_response.json.return_value = []
        get.side_effect = [ConnectionError("reset"), csv_response, tpex_response]

        result = _fetch_tw_official_bulk()

        self.assertEqual(result["0050"]["current_price"], 103.8)
        self.assertEqual(result["0050"]["quote_source"], "tw_official_csv")
        self.assertEqual(result["0050"]["quote_date"], date(2026, 8, 5))

    @patch("etf_market_data.req_lib.get")
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

    @patch("etf_market_data.req_lib.get")
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
