"""
routes/portfolio_routes.py — 庫存 / 交易記錄
"""
import logging
from datetime import date
from fastapi import APIRouter, Depends

from auth import get_current_user
from models import TransactionIn
from database import get_db
from utils import safe_json
from services.exchange_rate import get_usd_twd

logger = logging.getLogger(__name__)
router = APIRouter()

LATEST_DAILY_JOIN = """
LEFT JOIN (
    SELECT d1.* FROM etf_daily_data d1
    INNER JOIN (
        SELECT ticker, MAX(date) AS max_date FROM etf_daily_data GROUP BY ticker
    ) d2 ON d1.ticker = d2.ticker AND d1.date = d2.max_date
) d ON p.ticker = d.ticker
"""


@router.get("/api/portfolio")
async def get_portfolio(current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    usd_twd = get_usd_twd()

    with get_db() as (conn, cursor):
        cursor.execute(f"""
            SELECT p.ticker, m.name, m.market,
                p.shares, p.avg_cost,
                COALESCE(d.current_price, p.avg_cost) as current_price,
                COALESCE(d.dividend_yield, 0) as dividend_yield,
                COALESCE(d.payout_freq, '不配息') as payout_freq,
                COALESCE(d.price_change_percent, 0) as price_change_percent
            FROM user_portfolio p
            JOIN etf_master m ON p.ticker = m.ticker
            {LATEST_DAILY_JOIN}
            WHERE p.user_id=%s AND p.shares > 0
            ORDER BY (p.shares * COALESCE(d.current_price, p.avg_cost)) DESC
        """, (uid,))
        rows = cursor.fetchall()

    result, total_cost_twd, total_value_twd = [], 0.0, 0.0
    for r in rows:
        s    = float(r["shares"]  or 0)
        ac   = float(r["avg_cost"] or 0)
        cp   = float(r["current_price"] or ac)
        cost = s * ac
        val  = s * cp
        prof = val - cost
        ret  = prof / cost * 100 if cost > 0 else 0.0

        # TWD 換算
        fx = usd_twd if r["market"] == "US" else 1.0
        result.append({
            **r,
            "shares": s, "avg_cost": ac, "current_price": cp,
            "cost": round(cost, 2),
            "value": round(val, 2),
            "profit": round(prof, 2),
            "return_pct": round(ret, 2),
            "value_twd": round(val * fx, 0),
            "cost_twd": round(cost * fx, 0),
        })
        total_cost_twd  += cost * fx
        total_value_twd += val  * fx

    total_profit = total_value_twd - total_cost_twd
    total_return  = total_profit / total_cost_twd * 100 if total_cost_twd > 0 else 0.0

    return safe_json({
        "status": "success",
        "data": result,
        "summary": {
            "total_cost_twd":   round(total_cost_twd, 0),
            "total_value_twd":  round(total_value_twd, 0),
            "total_profit_twd": round(total_profit, 0),
            "total_return":     round(total_return, 2),
            "usd_twd_rate":     usd_twd,
        }
    })


@router.post("/api/portfolio/transaction")
async def add_transaction(body: TransactionIn, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    try:
        _insert_transaction(uid, body.dict())  # 內部已包含 recalc，單一 atomic transaction
        return safe_json({"status": "success", "message": "交易已新增"})
    except ValueError as e:
        return safe_json({"status": "error", "message": str(e)}, 400)
    except Exception as ex:
        logger.error(f"add_transaction: {ex}")
        return safe_json({"status": "error", "message": str(ex)}, 500)


@router.get("/api/portfolio/transactions")
async def get_transactions(ticker: str = None, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    with get_db() as (conn, cursor):
        if ticker:
            cursor.execute(
                "SELECT * FROM user_transactions WHERE user_id=%s AND ticker=%s ORDER BY transaction_date DESC, id DESC",
                (uid, ticker.upper())
            )
        else:
            cursor.execute(
                "SELECT * FROM user_transactions WHERE user_id=%s ORDER BY transaction_date DESC, id DESC",
                (uid,)
            )
        rows = cursor.fetchall()
    return safe_json({"status": "success", "data": rows})


@router.delete("/api/portfolio/transaction/{tid}")
async def delete_transaction(tid: int, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    with get_db() as (conn, cursor):
        cursor.execute("SELECT ticker FROM user_transactions WHERE id=%s AND user_id=%s", (tid, uid))
        row = cursor.fetchone()
        if not row:
            return safe_json({"status": "error", "message": "找不到此交易"}, 404)
        ticker = row["ticker"]
        cursor.execute("DELETE FROM user_transactions WHERE id=%s AND user_id=%s", (tid, uid))
        # 刪除與重算在同一 transaction 內，防止 race condition
        _recalc_portfolio_cursor(uid, ticker, cursor)
        conn.commit()
    return safe_json({"status": "success", "message": "已刪除"})


# ── 私有邏輯 ──

def _recalc_portfolio_cursor(uid: int, ticker: str, cursor):
    """重算持倉的核心邏輯（使用既有 cursor），供原子性操作共用。"""
    cursor.execute(
        "SELECT transaction_type, shares, price FROM user_transactions "
        "WHERE user_id=%s AND ticker=%s ORDER BY transaction_date ASC, id ASC",
        (uid, ticker)
    )
    txs = cursor.fetchall()

    total_shares = 0.0
    total_cost   = 0.0
    for t in txs:
        s = float(t["shares"])
        p = float(t["price"])
        if t["transaction_type"] == "buy":
            total_cost   += s * p
            total_shares += s
        elif t["transaction_type"] == "sell":
            if total_shares > 0:
                total_cost -= (total_cost / total_shares) * s
            total_shares -= s
            if total_shares < 0 or abs(total_shares) < 1e-6:
                total_shares = 0.0
                total_cost   = 0.0

    avg_cost = total_cost / total_shares if total_shares > 0 else 0.0

    if total_shares > 0:
        cursor.execute(
            "INSERT INTO user_portfolio (user_id,ticker,shares,avg_cost) VALUES (%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE shares=%s, avg_cost=%s",
            (uid, ticker, total_shares, avg_cost, total_shares, avg_cost)
        )
    else:
        cursor.execute("DELETE FROM user_portfolio WHERE user_id=%s AND ticker=%s", (uid, ticker))


def _insert_transaction(uid: int, data: dict):
    """新增交易並在同一 transaction 內重算持倉，防止 race condition。"""
    ticker  = data["ticker"].upper()
    tx_type = data["transaction_type"]
    shares  = float(data["shares"])
    price   = float(data["price"])
    comm    = float(data.get("commission", 0))
    tx_date = data["transaction_date"]
    note    = data.get("note", "") or ""

    with get_db() as (conn, cursor):
        # 確保 ETF master 存在
        cursor.execute("SELECT ticker FROM etf_master WHERE ticker=%s", (ticker,))
        if not cursor.fetchone():
            market = "TW" if ticker[:4].isdigit() else "US"
            cursor.execute(
                "INSERT OR IGNORE INTO etf_master (ticker,name,market) VALUES (%s,%s,%s)",
                (ticker, ticker, market)
            )

        # 賣出前檢查庫存（容許浮點誤差 1e-6，避免 99.999999 != 100.0 的誤判）
        if tx_type == "sell":
            cursor.execute("SELECT shares FROM user_portfolio WHERE user_id=%s AND ticker=%s", (uid, ticker))
            row = cursor.fetchone()
            held = float(row["shares"]) if row else 0.0
            if held < shares - 1e-6:
                raise ValueError(f"庫存不足：持有 {held:.4f}，欲賣 {shares:.4f}")

        cursor.execute(
            "INSERT INTO user_transactions (user_id,ticker,transaction_type,shares,price,commission,transaction_date,note) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (uid, ticker, tx_type, shares, price, comm, tx_date, note)
        )
        # 在同一 transaction 內重算持倉，避免讀取到中間狀態
        _recalc_portfolio_cursor(uid, ticker, cursor)
        conn.commit()


def _recalc_portfolio(uid: int, ticker: str):
    """獨立重算版本（供需要獨立連線的場景使用）。"""
    ticker = ticker.upper()
    with get_db() as (conn, cursor):
        _recalc_portfolio_cursor(uid, ticker, cursor)
        conn.commit()
