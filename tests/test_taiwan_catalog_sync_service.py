import unittest

from services.taiwan_catalog_sync_service import _parse_twse_products_csv


class TwseProductsCsvTest(unittest.TestCase):
    def test_parses_official_metrics_and_active_etf_code(self):
        csv_text = (
            '"ETF 投資篩選器"\r\n'
            '"股票代號","ETF名稱","上市日期","標的指數","資產規模(億元)",'
            '"收盤價","年初至今日均成交值(百萬元)","年初至今日均成交量(股)",'
            '"受益人數(人)","發行人",\r\n'
            '="00403A","主動統一升級50","2026.05.12","","1,549","9.52",'
            '"7,361.084","706,544,728","997,455","統一投信",\r\n'
        )

        rows = _parse_twse_products_csv(csv_text.encode("cp950"))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "00403A")
        self.assertEqual(rows[0]["fund_asset_size"], 154_900_000_000)
        self.assertEqual(rows[0]["holder_count"], 997_455)
        self.assertEqual(rows[0]["listing_date"], "2026-05-12")

    def test_accepts_utf8_csv_and_rejects_maintenance_html(self):
        csv_text = (
            '"ETF 投資篩選器"\n'
            '"股票代號","ETF名稱","上市日期","標的指數","資產規模(億元)",'
            '"收盤價","成交值","成交量","受益人數(人)","發行人"\n'
            '="0050","元大台灣50","2003.06.25","","100","100",'
            '"1","1","10","元大投信"\n'
        )

        rows = _parse_twse_products_csv(csv_text.encode("utf-8"))

        self.assertEqual(rows[0]["ticker"], "0050")
        self.assertEqual(
            _parse_twse_products_csv(b"<!DOCTYPE html><title>maintenance</title>"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
