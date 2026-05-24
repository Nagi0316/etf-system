"""
services/us_history.py — 補齊 US ETF 歷史收盤價

資料來源：Yahoo Finance v8 chart API（透過 CF Worker 代理，繞過 Railway IP 封鎖）
  URL 格式：https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=5y&interval=1d

策略：
  - 一次抓取 5 年日線（約 1250 筆），只補缺失日期，冪等安全
  - 已有資料的日期跳過（ON DUPLICATE KEY UPDATE 以 current_price=0 為條件）
  - 速率限制：每次請求後 sleep 1.5s，保守避免 Yahoo 429
  - 一次最多處理 MAX_ETFS 檔（可分批執行）

呼叫方式：
  from services.us_history import backfill_us_history
  backfill_us_history()         # 補所有 is_hot=1 且 market='US' 的 ETF
  backfill_us_history("SCHD")   # 只補特定 ETF
"""

import logging
import time
from datetime import date, datetime

from database import get_db
from etf_data import _cf_yahoo_get

logger = logging.getLogger(__name__)

_YF_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=5y&interval=1d&events=history"
_SLEEP   = 1.5   # seconds between tickers（Yahoo 對 CF 代理仍可能有速率限制）
MAX_ETFS = 200   # safety cap per run


# ── 核心抓取：單一 ETF 全部 5Y 日線 ──

def _fetch_history(ticker: str) -> list[dict]:
    """透過 CF 代理從 Yahoo Finance 抓取 5 年日線資料。
    回傳 [{"date": "2021-01-04", "close": 72.50, "volume": 12345678}, ...]
    """
    url = _YF_CHART_URL.format(ticker=ticker)
    resp = _cf_yahoo_get(url, timeout=20)
    if resp is None:
        logger.debug(f"US history {ticker}: CF proxy unavailable")
        return []
    try:
        body = resp.json()
        result_list = body.get("chart", {}).get("result")
        if not result_list:
            logger.debug(f"US history {ticker}: empty chart result")
            return []
        result = result_list[0]
        timestamps = result.get("timestamp", [])
        indicators  = result.get("indicators", {})
        quotes      = indicators.get("quote", [{}])[0]
        closes      = quotes.get("close", [])
        volumes     = quotes.get("volume", [])

        days = []
        for i, ts in enumerate(timestamps):
            try:
                close = closes[i]
                if close is None:
                    continue
                vol = volumes[i] if i < len(volumes) and volumes[i] is not None else 0
                iso_date = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                days.append({"date": iso_date, "close": float(close), "volume": int(vol)})
            except Exception:
                continue
        return days
    except Exception as e:
        logger.debug(f"US history {ticker} parse error: {e}")
        return []


# ── 查詢 DB 中已有的日期集合 ──

def _batch_existing_dates(tickers: list[str]) -> dict[str, set[str]]:
    """批次查詢多個 US ETF 已有資料的日期（ISO 字串 YYYY-MM-DD）。
    回傳 {ticker: {"2021-01-04", ...}}。
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
        result.setdefault(t, set()).add(d)
    return result


# ── 寫入 DB ──

def _save_days(ticker: str, days: list[dict]):
    """批次寫入歷史日資料（不覆蓋已有非零價格）。"""
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

def _backfill_one(ticker: str, existing: set | None = None) -> int:
    """抓取並補齊單一 US ETF 的歷史資料。回傳新補寫的天數。
    existing: 外部預先查好的已有日期 set；None 則代表第一次跑，視為無資料。
    """
    days = _fetch_history(ticker)
    if not days:
        return 0

    if existing is None:
        existing = set()

    new_days = [d for d in days if d["date"] not in existing]
    if new_days:
        _save_days(ticker, new_days)
        logger.debug(f"  {ticker}: 補 {len(new_days)} 日（共 {len(days)} 日取回）")
    return len(new_days)


# ── 公開入口 ──

def backfill_us_history(ticker: str = None) -> dict:
    """補齊 US ETF 歷史收盤價（5 年日線，透過 CF 代理）。

    Args:
        ticker: 指定補單一 ETF；None 表示補所有 is_hot=1 且 market='US' 的 ETF

    Returns:
        {"etfs": N, "days_inserted": M}
    """
    with get_db() as (conn, cursor):
        if ticker:
            cursor.execute(
                "SELECT ticker FROM etf_master WHERE ticker=%s AND market='US' AND is_delisted=0",
                (ticker.upper(),),
            )
        else:
            cursor.execute(
                "SELECT ticker FROM etf_master WHERE market='US' AND is_hot=1 AND is_delisted=0 "
                "ORDER BY ticker ASC LIMIT %s",
                (MAX_ETFS,),
            )
        tickers = [r["ticker"] for r in cursor.fetchall()]

    if not tickers:
        logger.warning("backfill_us_history: 沒有找到目標 US ETF")
        return {"etfs": 0, "days_inserted": 0}

    logger.info(f"🗓️  開始補 US ETF 歷史資料：{len(tickers)} 檔")
    existing_map = _batch_existing_dates(tickers)
    logger.info(f"📦 已批次查詢 {len(tickers)} 檔現有日期資料")

    total_etfs = 0
    total_days = 0

    for t in tickers:
        inserted = _backfill_one(t, existing=existing_map.get(t, set()))
        if inserted:
            logger.info(f"✅ {t}: 補 {inserted} 日")
        total_etfs += 1
        total_days += inserted
        time.sleep(_SLEEP)

    logger.info(f"🎉 US 歷史補齊完成：{total_etfs} 檔，共補 {total_days} 日資料")
    return {"etfs": total_etfs, "days_inserted": total_days}
