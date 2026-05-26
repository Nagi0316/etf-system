"""
services/returns_calc.py — 從 DB 歷史收盤價計算年化報酬率

設計原則：
  - 完全不依賴 Yahoo Finance，直接讀取 etf_daily_data 已有的收盤價
  - 對 TW ETF 尤其重要：Railway 上 Yahoo Finance 常被封鎖
  - 批次查詢（3 次 DB round-trip），不是每檔各打一次
  - 冪等：重複執行只會覆蓋最新一筆，不會新增或修改歷史行
  - 容錯：某檔計算失敗不影響其他檔

呼叫方式：
  from services.returns_calc import recalc_all_returns
  recalc_all_returns()          # 更新所有熱門 ETF
  recalc_all_returns("TW")      # 只算台股
"""
import logging
from collections import defaultdict
from datetime import date
from dateutil.relativedelta import relativedelta

from database import get_db

logger = logging.getLogger(__name__)


def _compute_annualized(price_now: float, price_then: float, years: float) -> float | None:
    """計算年化報酬率（%）。任一價格 ≤ 0 或 years ≤ 0 回傳 None。"""
    if not price_now or not price_then or price_now <= 0 or price_then <= 0 or years <= 0:
        return None
    try:
        total = price_now / price_then - 1
        if years <= 1:                           # < 1 年：直接用累計報酬
            return round(total * 100, 2)
        return round(((1 + total) ** (1 / years) - 1) * 100, 2)
    except Exception:
        return None


def _batch_prices_around_target(tickers: list[str], target_date: date,
                                 window_days: int = 60) -> dict[str, float]:
    """批次取每個 ticker 在 target_date ± window_days 內最接近 target_date 的收盤價。
    一次 DB query 處理所有 ticker，不逐檔打。
    回傳 {ticker: price}
    """
    if not tickers:
        return {}

    lo = (target_date - relativedelta(days=window_days)).isoformat()
    hi = (target_date + relativedelta(days=window_days)).isoformat()
    fmt = ",".join(["%s"] * len(tickers))

    with get_db() as (conn, cursor):
        cursor.execute(
            f"SELECT ticker, date, current_price FROM etf_daily_data "
            f"WHERE ticker IN ({fmt}) AND current_price > 0 "
            f"AND date BETWEEN %s AND %s "
            f"ORDER BY ticker, date",
            tickers + [lo, hi],
        )
        rows = cursor.fetchall()

    # 每個 ticker 選距離 target_date 最近的那筆
    grouped: dict[str, list] = defaultdict(list)
    for r in rows:
        grouped[r["ticker"]].append(r)

    result: dict[str, float] = {}
    for ticker, trows in grouped.items():
        best = min(
            trows,
            key=lambda r: abs((
                (r["date"] if isinstance(r["date"], date)
                 else date.fromisoformat(str(r["date"])[:10]))
                - target_date
            ).days),
        )
        gap = abs((
            (best["date"] if isinstance(best["date"], date)
             else date.fromisoformat(str(best["date"])[:10]))
            - target_date
        ).days)
        if gap <= window_days:
            result[ticker] = float(best["current_price"])

    return result


def recalc_all_returns(market: str | None = None) -> dict:
    """從 etf_daily_data 已有的收盤價，重新計算所有熱門 ETF 的年化報酬率。

    邏輯：
      1. 查各 ticker 最新一筆有效收盤（current_price > 0）
      2. 批次查 1Y / 3Y / 5Y 前的收盤（各一次 DB query）
      3. 計算年化報酬，UPDATE 最新一筆的 annual_return_*

    Args:
        market: "TW" / "US" / None（None = 全部）

    Returns:
        {"updated": N, "skipped": N}
    """
    today = date.today()

    # ── Step 1: 取熱門 ETF 清單 ──
    with get_db() as (conn, cursor):
        if market:
            cursor.execute(
                "SELECT ticker FROM etf_master "
                "WHERE is_hot=1 AND COALESCE(is_delisted,0)=0 AND market=%s",
                (market.upper(),),
            )
        else:
            cursor.execute(
                "SELECT ticker FROM etf_master "
                "WHERE is_hot=1 AND COALESCE(is_delisted,0)=0"
            )
        tickers = [r["ticker"] for r in cursor.fetchall()]

    if not tickers:
        logger.warning("recalc_all_returns: 無熱門 ETF")
        return {"updated": 0, "skipped": 0}

    logger.info(f"🔢 開始計算 {len(tickers)} 檔 ETF 年化報酬率（從 DB 歷史價格）")
    fmt = ",".join(["%s"] * len(tickers))

    # ── Step 2: 查各 ticker 最新收盤 ──
    with get_db() as (conn, cursor):
        cursor.execute(
            f"""SELECT d.ticker, d.date, d.current_price
                FROM etf_daily_data d
                INNER JOIN (
                    SELECT ticker, MAX(date) AS max_date
                    FROM etf_daily_data
                    WHERE ticker IN ({fmt}) AND current_price > 0
                    GROUP BY ticker
                ) m ON d.ticker = m.ticker AND d.date = m.max_date""",
            tickers,
        )
        latest_map = {r["ticker"]: r for r in cursor.fetchall()}

    if not latest_map:
        logger.warning("recalc_all_returns: 無任何最新收盤資料")
        return {"updated": 0, "skipped": len(tickers)}

    # ── Step 3: 批次查 1Y / 3Y / 5Y 前的收盤 ──
    # 以 DB 中最新資料日期為基準（而非 today），避免週末/假日造成期間偏差 0–3 個交易日
    reference_date = today
    if latest_map:
        dates_in_map = []
        for r in latest_map.values():
            d = r["date"] if isinstance(r["date"], date) else date.fromisoformat(str(r["date"])[:10])
            dates_in_map.append(d)
        reference_date = max(dates_in_map)
        if reference_date != today:
            logger.debug(f"recalc_all_returns: reference_date={reference_date}（非 today={today}，差 {(today - reference_date).days} 日）")

    prices_1y = _batch_prices_around_target(tickers, reference_date - relativedelta(years=1))
    prices_3y = _batch_prices_around_target(tickers, reference_date - relativedelta(years=3))
    prices_5y = _batch_prices_around_target(tickers, reference_date - relativedelta(years=5))

    logger.debug(f"  歷史報酬：1Y={len(prices_1y)}檔 3Y={len(prices_3y)}檔 5Y={len(prices_5y)}檔")

    # ── Step 4: 計算 + 批次 UPDATE ──
    updated = 0
    skipped = 0

    for ticker in tickers:
        info = latest_map.get(ticker)
        if not info:
            skipped += 1
            continue

        now_price = float(info["current_price"])
        latest_date = (info["date"] if isinstance(info["date"], date)
                       else date.fromisoformat(str(info["date"])[:10]))

        r1y = _compute_annualized(now_price, prices_1y.get(ticker), 1)
        r3y = _compute_annualized(now_price, prices_3y.get(ticker), 3)
        r5y = _compute_annualized(now_price, prices_5y.get(ticker), 5)

        if r1y is None and r3y is None and r5y is None:
            skipped += 1
            continue

        # 只寫非 NULL 欄位，不把已有值蓋成 NULL
        cols, vals = [], []
        if r1y is not None:
            cols.append("annual_return_1y=%s"); vals.append(r1y)
        if r3y is not None:
            cols.append("annual_return_3y=%s"); vals.append(r3y)
        if r5y is not None:
            cols.append("annual_return_5y=%s"); vals.append(r5y)

        vals += [ticker, latest_date.isoformat()]

        try:
            with get_db() as (conn, cursor):
                cursor.execute(
                    f"UPDATE etf_daily_data SET {', '.join(cols)} "
                    f"WHERE ticker=%s AND date=%s",
                    vals,
                )
                conn.commit()
            logger.debug(f"  {ticker}: 1y={r1y}% 3y={r3y}% 5y={r5y}%")
            updated += 1
        except Exception as e:
            logger.warning(f"  recalc {ticker}: {e}")
            skipped += 1

    logger.info(f"✅ 年化報酬率重算完成：更新 {updated} 檔，略過 {skipped} 檔")
    return {"updated": updated, "skipped": skipped}
