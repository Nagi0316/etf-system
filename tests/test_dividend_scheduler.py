import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database
import etf_data
import scheduler


class DividendGapSchedulerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "dividend-gaps.db")
        self.db_patches = [
            patch.object(database, "USE_MYSQL", False),
            patch.object(database, "SQLITE_PATH", self.db_path),
        ]
        for item in self.db_patches:
            item.start()
        database.init_db()

        with database.get_db() as (conn, cursor):
            cursor.executemany(
                "INSERT INTO etf_master "
                "(ticker, name, market, is_hot, is_delisted) "
                "VALUES (%s, %s, %s, %s, %s)",
                [
                    ("0050", "ETF 0050", "TW", 1, 0),
                    ("0056", "ETF 0056", "TW", 1, 0),
                    ("00878", "ETF 00878", "TW", 1, 0),
                ],
            )
            cursor.executemany(
                "INSERT INTO etf_daily_data "
                "(ticker, date, current_price, dividend_status) "
                "VALUES (%s, %s, %s, %s)",
                [
                    ("0050", "2026-08-05", 100, "unknown"),
                    ("0056", "2026-08-05", 50, "unknown"),
                    ("00878", "2026-08-05", 20, "unknown"),
                ],
            )
            cursor.execute(
                "INSERT INTO dividend_sync_state "
                "(ticker, last_attempt_at, last_status) VALUES (%s, %s, %s)",
                ("0050", "2026-08-05 08:00:00", "unknown"),
            )
            conn.commit()

    def tearDown(self):
        for item in reversed(self.db_patches):
            item.stop()
        self.tmp.cleanup()

    async def test_unattempted_tickers_are_not_starved_by_old_failures(self):
        seen = []

        def fetch(ticker, market, price):
            seen.append(ticker)
            return {"dividend_status": "unknown"}

        with (
            patch.object(etf_data, "fetch_dividend_only", side_effect=fetch),
            patch.object(etf_data, "save_dividend_snapshot", return_value=False),
            patch.object(scheduler.asyncio, "sleep", new=AsyncMock()),
        ):
            await scheduler._update_dividend_gaps(limit=2)

        self.assertEqual(seen, ["0056", "00878"])
        with database.get_db() as (_, cursor):
            cursor.execute(
                "SELECT ticker FROM dividend_sync_state ORDER BY ticker"
            )
            attempted = [row["ticker"] for row in cursor.fetchall()]
        self.assertEqual(attempted, ["0050", "0056", "00878"])


class DividendOnlyFetchTest(unittest.TestCase):
    def test_us_dividend_gap_uses_us_pipeline(self):
        with patch.object(
            etf_data,
            "_fetch_us_history_dividend",
            return_value=([], 1.25, "季配", True),
        ):
            result = etf_data.fetch_dividend_only("SPY", "US", 700)

        self.assertEqual(result["dividend_yield"], 1.25)
        self.assertEqual(result["dividend_status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
