import unittest
from unittest.mock import Mock, patch

from services.us_history import _fetch_nasdaq_history


class UsHistorySourceTest(unittest.TestCase):
    @patch("services.us_history.requests.get")
    def test_parses_nasdaq_history_in_chronological_order(self, get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "tradesTable": {
                    "rows": [
                        {
                            "date": "07/24/2026", "close": "$738.93",
                            "volume": "44,781,980",
                        },
                        {
                            "date": "07/23/2026", "close": "$738.10",
                            "volume": "40,000,000",
                        },
                    ]
                }
            }
        }
        get.return_value = response

        result = _fetch_nasdaq_history("SPY")

        self.assertEqual([row["date"] for row in result], ["2026-07-23", "2026-07-24"])
        self.assertEqual(result[-1]["close"], 738.93)
        self.assertEqual(result[-1]["volume"], 44_781_980)


if __name__ == "__main__":
    unittest.main()
