"""
routes/watchlist_routes.py — ETF 自選清單
"""
import logging
from fastapi import APIRouter, Depends

from auth import get_current_user
from models import WatchlistAddIn
from database import get_db
from utils import safe_json

logger = logging.getLogger(__name__)
router = APIRouter()

LATEST_DAILY_JOIN = """
LEFT JOIN (
    SELECT d1.* FROM etf_daily_data d1
    INNER JOIN (
        SELECT ticker, MAX(date) AS max_date FROM etf_daily_data GROUP BY ticker
    ) d2 ON d1.ticker = d2.ticker AND d1.date = d2.max_date
) d ON w.ticker = d.ticker
"""


@router.get("/api/watchlist")
async def get_watchlist(current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    with get_db() as (conn, cursor):
        cursor.execute(f"""
            SELECT w.ticker, m.name, m.market,
                COALESCE(d.current_price,0) as current_price,
                COALESCE(d.price_change,0) as price_change,
                COALESCE(d.price_change_percent,0) as price_change_percent,
                COALESCE(d.payout_freq,'不配息') as payout_freq,
                COALESCE(d.volume,0) as volume,
                COALESCE(d.day_high,0) as day_high,
                COALESCE(d.day_low,0) as day_low,
                COALESCE(d.dividend_yield,0) as dividend_yield,
                COALESCE(d.annual_return_1y,0) as annual_return_1y
            FROM user_watchlist w
            JOIN etf_master m ON w.ticker = m.ticker
            {LATEST_DAILY_JOIN}
            WHERE w.user_id=%s ORDER BY w.added_at DESC
        """, (uid,))
        rows = cursor.fetchall()
    return safe_json({"status": "success", "data": rows})


@router.post("/api/watchlist/add")
async def add_watchlist(body: WatchlistAddIn, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    ticker = body.ticker
    market = body.market or ("TW" if ticker[:4].isdigit() else "US")
    name   = body.name or ticker

    with get_db() as (conn, cursor):
        cursor.execute("SELECT ticker FROM etf_master WHERE ticker=%s", (ticker,))
        if not cursor.fetchone():
            cursor.execute(
                "INSERT OR REPLACE INTO etf_master (ticker,name,market) VALUES (%s,%s,%s)",
                (ticker, name, market)
            )
        cursor.execute("SELECT id FROM user_watchlist WHERE user_id=%s AND ticker=%s", (uid, ticker))
        if cursor.fetchone():
            return safe_json({"status": "error", "message": "已在自選清單中"}, 400)
        cursor.execute("INSERT INTO user_watchlist (user_id,ticker) VALUES (%s,%s)", (uid, ticker))
        conn.commit()
    return safe_json({"status": "success", "message": f"已加入自選：{ticker}"})


@router.get("/api/watchlist/check/{ticker}")
async def check_watchlist(ticker: str, current_user: dict = Depends(get_current_user)):
    """輕量查詢：單一 ticker 是否在自選清單中。"""
    uid = current_user["id"]
    with get_db() as (conn, cursor):
        cursor.execute(
            "SELECT 1 FROM user_watchlist WHERE user_id=%s AND ticker=%s",
            (uid, ticker.upper())
        )
        exists = cursor.fetchone() is not None
    return safe_json({"status": "success", "in_watchlist": exists})


@router.delete("/api/watchlist/remove/{ticker}")
async def remove_watchlist(ticker: str, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    with get_db() as (conn, cursor):
        cursor.execute("DELETE FROM user_watchlist WHERE user_id=%s AND ticker=%s", (uid, ticker.upper()))
        conn.commit()
    return safe_json({"status": "success", "message": "已移除"})
