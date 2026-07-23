"""
services/twse_sync.py — 自動同步台灣全市場 ETF 清單

資料來源：
  - 臺灣證券交易所 ETF 投資篩選器 CSV（完整 ETF 清單、資產規模、受益人數）
  - 台灣證券交易所 OpenAPI（上市）
  - 證券櫃檯買賣中心 OpenAPI（上櫃）

每天早上 08:00 執行：
  1. 新增 etf_master 尚未存在的代碼
  2. 將「今日交易所清單上已無」且「auto_discovered=1」的代碼標記為 is_delisted=1
"""
import csv
import io
import logging
from datetime import date

import requests

from cache import cache
from database import get_db

logger = logging.getLogger(__name__)

TWSE_URL          = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL          = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
TWSE_FUND_URL     = "https://openapi.twse.com.tw/v1/opendata/t187ap47_L"
TWSE_PRODUCTS_URL = "https://www.twse.com.tw/zh/ETFortune-institute/exportProducts"
TWSE_PRODUCTS_PAGE = "https://www.twse.com.tw/zh/ETFortune-institute/products"

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; ETF-System/2.0)",
}
_TIMEOUT = 20


def _number(raw, multiplier: float = 1.0) -> float:
    try:
        return float(str(raw or "").replace(",", "").strip()) * multiplier
    except (TypeError, ValueError):
        return 0.0


def _parse_twse_products_csv(content: bytes) -> list[dict]:
    """解析證交所 ETF 投資篩選器 CSV（CP950 編碼）。

    CSV 第一列是報表名稱、第二列才是欄名；代碼使用 Excel 公式格式
    ``="0050"``，必須移除包裝後再寫入資料庫。
    """
    text = content.decode("cp950")
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 3:
        return []

    result: list[dict] = []
    for row in rows[2:]:
        if len(row) < 10:
            continue
        ticker = row[0].strip()
        if ticker.startswith('="') and ticker.endswith('"'):
            ticker = ticker[2:-1]
        ticker = ticker.strip().upper()
        if not ticker or not ticker.isalnum() or not (4 <= len(ticker) <= 7):
            continue

        listing_date = row[2].strip().replace(".", "-") or None
        result.append({
            "ticker": ticker,
            "name": row[1].strip() or ticker,
            "market": "TW",
            "listing_date": listing_date,
            # 官方 CSV 單位為億元，DB 統一儲存元。
            "fund_asset_size": round(_number(row[4], 100_000_000), 2),
            "holder_count": int(_number(row[8])),
            "issuer": row[9].strip(),
        })
    return result


def _fetch_twse_products() -> list[dict]:
    """取得證交所完整 ETF 商品及排行所需的官方低頻指標。"""
    try:
        headers = {**_HEADERS, "Referer": TWSE_PRODUCTS_PAGE}
        resp = requests.get(TWSE_PRODUCTS_URL, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        result = _parse_twse_products_csv(resp.content)
        logger.info(f"TWSE ETF 投資篩選器: 取得 {len(result)} 檔商品指標")
        return result
    except Exception as e:
        logger.warning(f"TWSE ETF 投資篩選器 CSV 失敗: {e}")
        return []


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


def _fetch_twse_outstanding_units() -> dict:
    """從 TWSE OpenAPI t187ap47_L 取得所有台股 ETF 的發行單位數。
    回傳 {ticker: units_int}，供 sync_tw_etfs() 更新 etf_master.outstanding_units。
    """
    try:
        resp = requests.get(TWSE_FUND_URL, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        result = {}
        for item in resp.json():
            code = str(item.get("基金代號", "")).strip()
            raw  = str(item.get("發行單位數/轉換數", "0")).replace(",", "").strip()
            try:
                units = int(float(raw))
                if code and units > 0:
                    result[code] = units
            except (ValueError, TypeError):
                pass
        logger.info(f"TWSE t187ap47_L: 取得 {len(result)} 筆 ETF 發行單位數")
        return result
    except Exception as e:
        logger.warning(f"TWSE 發行單位數 API 失敗: {e}")
        return {}


def sync_tw_etfs() -> int:
    """同步台灣全市場 ETF 代碼到 etf_master。

    1. INSERT IGNORE 新代碼（不修改現有資料）
    2. 對「auto_discovered=1 但今日不在交易所清單」的代碼標記 is_delisted=1
    回傳新增筆數。
    """
    product_metrics = _fetch_twse_products()
    # 投資篩選器是官方 ETF 專用清單，包含 004xx 主動式 ETF；
    # STOCK_DAY_ALL 的「00 開頭」規則僅作為 CSV 暫時失敗時的備援。
    twse_list = [
        (item["ticker"], item["name"], "TW")
        for item in product_metrics
    ] or _fetch_twse()
    tpex_list = _fetch_tpex()

    if not twse_list and not tpex_list:
        logger.error(
            "上市與上櫃 ETF 來源均失敗，跳過同步以保護資料庫"
        )
        return 0
    sources_complete = bool(twse_list) and bool(tpex_list)

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
        # ── 1. 新增不存在的代碼，並更新官方名稱 ──
        cursor.executemany(
            "INSERT IGNORE INTO etf_master "
            "(ticker, name, market, auto_discovered, is_delisted) "
            "VALUES (%s, %s, %s, 1, 0)",
            deduped,
        )
        new_count = max(cursor.rowcount, 0)
        cursor.executemany(
            "UPDATE etf_master SET name=%s, market=%s, is_delisted=0 "
            "WHERE ticker=%s",
            [(name, market, ticker) for ticker, name, market in deduped],
        )

        # ── 2. 寫入官方資產規模與受益人數 ──
        metrics_day = date.today().isoformat()
        if product_metrics:
            cursor.executemany(
                "UPDATE etf_master SET issuer=%s, listing_date=%s, "
                "fund_asset_size=%s, holder_count=%s, metrics_date=%s "
                "WHERE ticker=%s",
                [
                    (
                        item["issuer"], item["listing_date"],
                        item["fund_asset_size"], item["holder_count"],
                        metrics_day, item["ticker"],
                    )
                    for item in product_metrics
                ],
            )

        # ── 3. 標記「不在今日清單且 auto_discovered=1」的代碼為疑似下市 ──
        # 只有上市、上櫃兩個來源都成功時才做，避免單一來源故障造成誤下市。
        if active_codes and sources_complete:
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

    logger.info(
        f"✅ sync_tw_etfs: 掃描 {len(deduped)} 檔，新增 {new_count} 檔，"
        f"官方指標 {len(product_metrics)} 檔"
    )

    # ── 更新 ETF 發行單位數（用於計算基金規模，補 Yahoo Finance 空缺）──
    units_map = _fetch_twse_outstanding_units()
    if units_map:
        with get_db() as (conn, cursor):
            cursor.executemany(
                "UPDATE etf_master SET outstanding_units=%s WHERE ticker=%s",
                [(units, ticker) for ticker, units in units_map.items()],
            )
            updated = max(cursor.rowcount, 0)
            conn.commit()
        logger.info(f"✅ 更新 {updated} 檔 ETF 發行單位數（基金規模計算依據）")

    cache.delete("etf:index")
    cache.delete_prefix("search:")
    cache.delete_prefix("rank:")
    return new_count
