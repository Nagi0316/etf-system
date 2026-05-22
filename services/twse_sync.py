"""
services/twse_sync.py — 自動同步台灣全市場 ETF 清單

資料來源：
  - 台灣證券交易所 OpenAPI（上市）
  - 證券櫃檯買賣中心 OpenAPI（上櫃）

每天早上 08:00 執行：
  1. 新增 etf_master 尚未存在的代碼
  2. 將「今日交易所清單上已無」且「auto_discovered=1」的代碼標記為 is_delisted=1
"""
import logging
import requests
from database import get_db

logger = logging.getLogger(__name__)

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; ETF-System/2.0)",
}
_TIMEOUT = 20


def _is_tw_etf_code(code: str) -> bool:
    """判斷台股代碼是否為 ETF。
    台灣 ETF 代碼規則：
      - 4-7 個字元
      - 通常以 "00" 開頭（00878、006208、00679B）
      - 允許末尾一個英文字母（B=債券、L=槓桿、R=反向、U=美元計價 等）
    """
    if not code:
        return False
    code = code.strip()
    if not (4 <= len(code) <= 7):
        return False
    if not code.startswith("00"):
        return False
    body = code.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
    return body.isdigit() and len(body) >= 4


def _fetch_twse() -> list[tuple]:
    """從台灣證交所 OpenAPI 取得所有上市 ETF 代碼與名稱"""
    try:
        resp = requests.get(TWSE_URL, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        result = []
        for item in data:
            code = str(item.get("Code", "")).strip()
            name = str(item.get("Name", "")).strip()
            if _is_tw_etf_code(code):
                result.append((code, name, "TW"))
        logger.info(f"TWSE: 取得 {len(result)} 檔 ETF 代碼")
        return result
    except Exception as e:
        logger.warning(f"TWSE OpenAPI 失敗: {e}")
        return []


def _fetch_tpex() -> list[tuple]:
    """從證券櫃檯買賣中心 OpenAPI 取得上櫃 ETF"""
    try:
        resp = requests.get(TPEX_URL, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        result = []
        for item in data:
            code = str(item.get("SecuritiesCompanyCode", "")).strip()
            name = str(item.get("CompanyName", "")).strip()
            if _is_tw_etf_code(code):
                result.append((code, name, "TW"))
        logger.info(f"TPEX: 取得 {len(result)} 檔 ETF 代碼")
        return result
    except Exception as e:
        logger.warning(f"TPEX OpenAPI 失敗: {e}")
        return []


def sync_tw_etfs() -> int:
    """同步台灣全市場 ETF 代碼到 etf_master。

    1. INSERT IGNORE 新代碼（不修改現有資料）
    2. 對「auto_discovered=1 但今日不在交易所清單」的代碼標記 is_delisted=1
    回傳新增筆數。
    """
    twse_list = _fetch_twse()
    tpex_list = _fetch_tpex()

    if not twse_list or not tpex_list:
        logger.error(
            f"上市或上櫃 API 抓取失敗（TWSE:{len(twse_list)} / TPEX:{len(tpex_list)}），"
            "跳過下市判斷以保護資料庫"
        )
        return 0

    etf_list = twse_list + tpex_list

    # 去重
    seen: set[str] = set()
    deduped: list[tuple] = []
    for row in etf_list:
        if row[0] not in seen:
            seen.add(row[0])
            deduped.append(row)

    active_codes = {row[0] for row in deduped}

    new_count = 0
    with get_db() as (conn, cursor):
        # ── 1. 新增不存在的代碼 ──
        for ticker, name, market in deduped:
            try:
                cursor.execute(
                    "INSERT IGNORE INTO etf_master (ticker, name, market, auto_discovered) "
                    "VALUES (%s, %s, %s, 1)",
                    (ticker, name, market),
                )
                new_count += cursor.rowcount
            except Exception as e:
                logger.debug(f"etf_master insert {ticker}: {e}")

        # ── 2. 復活曾被標記下市但今日重新出現的代碼 ──
        if active_codes:
            fmt = ",".join(["%s"] * len(active_codes))
            cursor.execute(
                f"UPDATE etf_master SET is_delisted=0 "
                f"WHERE market='TW' AND ticker IN ({fmt}) AND is_delisted=1",
                list(active_codes),
            )
            reactivated = cursor.rowcount
            if reactivated:
                logger.info(f"🔄 {reactivated} 檔 ETF 重新上市，已取消下市標記")

        # ── 3. 標記「不在今日清單且 auto_discovered=1」的代碼為疑似下市 ──
        if active_codes:
            fmt = ",".join(["%s"] * len(active_codes))
            cursor.execute(
                f"UPDATE etf_master SET is_delisted=1 "
                f"WHERE market='TW' AND auto_discovered=1 AND is_delisted=0 "
                f"AND ticker NOT IN ({fmt})",
                list(active_codes),
            )
            delisted_count = cursor.rowcount
            if delisted_count:
                logger.warning(
                    f"🗑️ 標記 {delisted_count} 檔不在今日交易所清單的台股 ETF 為疑似下市，"
                    "排行榜與搜尋將自動排除。"
                )

        conn.commit()

    logger.info(f"✅ sync_tw_etfs: 掃描 {len(deduped)} 檔，新增 {new_count} 檔進資料庫")
    return new_count
