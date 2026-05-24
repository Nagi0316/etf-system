"""
services/twse_history.py — 一次性補齊 TW ETF 歷史收盤價

資料來源：台灣證券交易所 (TWSE) 官方 OpenAPI
  個股日成交資訊：https://www.twse.com.tw/rwd/zh/stock/STOCK_DAY
  參數：stockNo=00878&date=20240101

策略：
  - 逐月抓取 ETF 上市後或 5 年內的歷史月份
  - 只補缺失月份，已有資料的月份跳過（冪等）
  - 速率限制：每次請求後 sleep 1.0s，避免封鎖
  - 一次最多處理 MAX_ETFS 檔（可分批執行）

呼叫方式：
  from services.twse_history import backfill_tw_history
  backfill_tw_history()          # 補所有 TW ETF
  backfill_tw_history("00878")   # 只補特定 ETF
"""

import logging
import time
import requests
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from database import get_db

logger = logging.getLogger(__name__)

TWSE_DAY_URL = "https://www.twse.com.tw/rwd/zh/stock/STOCK_DAY"
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; ETF-System/2.0)",
}
_TIMEOUT = 20
_SLEEP   = 0.15  # seconds between requests（TWSE 公開 API 限制寬鬆，0.15s 足夠）
MAX_ETFS = 200   # safety cap per run


# ── 核心抓取：單一 ETF 單一月份 ──

def _fetch_month(ticker: str, year: int, month: int) -> list[dict]:
    """從 TWSE 抓取某月份的日成交資料。
    回傳 [{"date": "2024-01-02", "close": 19.50, "volume": 1234567}, ...]
    """
    date_str = f"{year}{month:02d}01"
    params = {"stockNo": ticker, "date": date_str, "response": "json"}
    try:
        resp = requests.get(TWSE_DAY_URL, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        if body.get("stat") != "OK":
            return []
        rows = body.get("data", [])
        result = []
        for row in rows:
            # 欄位順序：日期 成交股數 成交金額 開盤價 最高價 最低價 收盤價 漲跌價差 成交筆數
            try:
                tw_date = row[0].strip()   # e.g. "113/01/02"
                close_s = row[6].strip().replace(",", "")
                volume_s = row[1].strip().replace(",", "")
                # 民國轉西元
                parts = tw_date.split("/")
                ad_year = int(parts[0]) + 1911
                iso_date = f"{ad_year}-{parts[1]}-{parts[2]}"
                close = float(close_s)
                volume = int(volume_s) if volume_s.lstrip("-").isdigit() else 0
                result.append({"date": iso_date, "close": close, "volume": volume})
            except Exception:
                continue
        return result
    except Exception as e:
        logger.debug(f"TWSE history {ticker} {year}/{month}: {e}")
        return []


# ── 查詢 DB 中哪些月份已有資料 ──

def _existing_months(ticker: str) -> set[tuple[int, int]]:
    """回傳 DB 中已有日資料的 (year, month) set。（單檔版本，供外部呼叫用）"""
    with get_db() as (conn, cursor):
        cursor.execute(
            "SELECT DISTINCT date FROM etf_daily_data WHERE ticker=%s AND current_price > 0",
            (ticker,),
        )
        rows = cursor.fetchall()
    months = set()
    for r in rows:
        d = str(r["date"])[:10]
        try:
            y, m = int(d[:4]), int(d[5:7])
            months.add((y, m))
        except Exception:
            pass
    return months


def _batch_existing_months(tickers: list[str]) -> dict[str, set[tuple[int, int]]]:
    """批次查詢多個 ticker 已有的月份（一次 DB 查詢，避免每檔各建一次連線）。
    回傳 {ticker: {(year, month), ...}}。
    """
    if not tickers:
        return {}
    with get_db() as (conn, cursor):
        fmt = ",".join(["%s"] * len(tickers))
        cursor.execute(
            f"SELECT DISTINCT ticker, date FROM etf_daily_data "
            f"WHERE ticker IN ({fmt}) AND current_price > 0",
            tickers,
        )
        rows = cursor.fetchall()
    result: dict[str, set] = {}
    for r in rows:
        t = r["ticker"]
        d = str(r["date"])[:10]
        try:
            y, m = int(d[:4]), int(d[5:7])
            result.setdefault(t, set()).add((y, m))
        except Exception:
            pass
    return result


# ── 寫入 DB ──

def _save_days(ticker: str, days: list[dict]):
    """將歷史日資料批次寫入 etf_daily_data（只寫 price / volume，不蓋掉其他欄位）。"""
    if not days:
        return
    with get_db() as (conn, cursor):
        for d in days:
            try:
                cursor.execute(
                    "INSERT INTO etf_daily_data (ticker, date, current_price, volume) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE "
                    "current_price = IF(current_price=0, VALUES(current_price), current_price), "
                    "volume        = IF(volume=0,         VALUES(volume),        volume)",
                    (ticker, d["date"], d["close"], d["volume"]),
                )
            except Exception as e:
                logger.debug(f"insert {ticker} {d['date']}: {e}")
        conn.commit()


# ── 單一 ETF 補齊 ──

def _backfill_one(ticker: str, since: date, until: date,
                  existing: set | None = None) -> int:
    """補齊單一 ETF 從 since 到 until 的每月歷史資料。回傳補寫的天數。
    existing: 外部預先查好的已有月份 set；None 則自行查詢（向下相容）。
    """
    if existing is None:
        existing = _existing_months(ticker)
    total_days = 0
    cur = date(since.year, since.month, 1)
    while cur <= until:
        ym = (cur.year, cur.month)
        if ym not in existing:
            days = _fetch_month(ticker, cur.year, cur.month)
            if days:
                _save_days(ticker, days)
                total_days += len(days)
                logger.debug(f"  {ticker} {cur.year}/{cur.month:02d}: 補 {len(days)} 日")
            time.sleep(_SLEEP)
        cur += relativedelta(months=1)
    return total_days


# ── 公開入口 ──

def backfill_tw_history(ticker: str = None, years: int = 5) -> dict:
    """補齊 TW ETF 歷史收盤價。

    Args:
        ticker: 指定補單一 ETF；None 表示補所有 TW ETF（上限 MAX_ETFS 檔）
        years:  回溯年數（預設 5 年）

    Returns:
        {"etfs": N, "days_inserted": M}
    """
    until = date.today()
    since = until - timedelta(days=365 * years)

    # 取得目標 ETF 清單
    with get_db() as (conn, cursor):
        if ticker:
            cursor.execute(
                "SELECT ticker FROM etf_master WHERE ticker=%s AND market='TW' AND is_delisted=0",
                (ticker.upper(),),
            )
        else:
            cursor.execute(
                "SELECT ticker FROM etf_master WHERE market='TW' AND is_delisted=0 "
                "ORDER BY is_hot DESC, ticker ASC LIMIT %s",
                (MAX_ETFS,),
            )
        tickers = [r["ticker"] for r in cursor.fetchall()]

    if not tickers:
        logger.warning("backfill_tw_history: 沒有找到目標 ETF")
        return {"etfs": 0, "days_inserted": 0}

    logger.info(f"🗓️  開始補歷史資料：{len(tickers)} 檔，回溯至 {since}")
    total_etfs = 0
    total_days = 0

    # 批次查詢所有 ticker 的已有月份（一次 DB 連線 vs 原本每檔一次連線）
    existing_map = _batch_existing_months(tickers)
    logger.info(f"📦 已批次查詢 {len(tickers)} 檔現有月份資料（節省 {len(tickers)-1} 次 DB 連線）")

    for t in tickers:
        inserted = _backfill_one(t, since, until, existing=existing_map.get(t, set()))
        if inserted:
            logger.info(f"✅ {t}: 補 {inserted} 日")
        total_etfs += 1
        total_days += inserted

    logger.info(f"🎉 歷史補齊完成：{total_etfs} 檔，共補 {total_days} 日資料")
    return {"etfs": total_etfs, "days_inserted": total_days}
