import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import database
import etf_data
from routes.etf_routes import _ranking_data_meta


class DataSyncStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "sync-state.db")
        self.patches = [
            patch.object(database, "USE_MYSQL", False),
            patch.object(database, "SQLITE_PATH", self.db_path),
        ]
        for item in self.patches:
            item.start()
        database.init_db()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_success_state_exposes_real_coverage_and_data_date(self):
        etf_data.record_sync_state("quotes", "TW", 2, {
            "0050": {"quote_date": date(2026, 8, 4)},
            "0056": {"quote_date": date(2026, 8, 4)},
        })

        with database.get_db() as (_, cursor):
            meta = _ranking_data_meta(cursor, "TW")

        self.assertEqual(meta["sync_status"], "success")
        self.assertEqual(meta["data_date"], "2026-08-04")
        self.assertEqual(meta["coverage"]["percent"], 100.0)

    def test_failed_attempt_does_not_erase_last_success(self):
        etf_data.record_sync_state("quotes", "TW", 1, {
            "0050": {"quote_date": date(2026, 8, 4)},
        })
        with database.get_db() as (_, cursor):
            cursor.execute(
                "SELECT last_success_at FROM data_sync_state "
                "WHERE dataset=%s AND market=%s", ("quotes", "TW")
            )
            successful_at = cursor.fetchone()["last_success_at"]

        etf_data.record_sync_state("quotes", "TW", 1, {})

        with database.get_db() as (_, cursor):
            cursor.execute(
                "SELECT status, last_success_at FROM data_sync_state "
                "WHERE dataset=%s AND market=%s", ("quotes", "TW")
            )
            row = cursor.fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["last_success_at"], successful_at)


if __name__ == "__main__":
    unittest.main()
