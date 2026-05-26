"""
routes/portfolio_routes.py — 庫存 / 交易記錄

冪等防護（雙層縱深）：
  1. Cache 層（5s TTL）：攔截雙擊 / 網路重傳
  2. DB 層（idempotency_key UNIQUE）：即使 cache miss 也能防住

均價計算原則：
  · 買入均價 = Σ(股數×股價 + 手續費) / 總股數       ← 含買入手續費
  · 賣出：從成本基礎中扣除「avg_cost × 賣出股數」
  · 賣出手續費 → 直接計入該筆「已實現損益」，不影響成本基礎
  · 已實現損益 = Σ [(賣出價 - avg_cost_at_sell) × 賣出股數 - 賣出手續費]

已實現損益儲存：
  · 每次 _recalc_portfolio_cursor 重算，結果寫入 user_portfolio.realized_profit
  · 倉位歸零時保留紀錄（不 DELETE），shares=0 仍可查歷史損益
"""
import hashlib
import logging
from datetime import date
from fastapi import APIRouter, Depends

from auth import get_current_user
from cache import cache
from models import TransactionIn
from database import get_db
from utils import safe_json
from services.exchange_rate import get_usd_twd

_DEDUP_TTL = 5   # cache 冪等視窗（秒）

logger = logging.getLogger(__name__)
router = APIRouter()

LATEST_DAILY_JOIN = """
LEFT JOIN (
    SELECT d1.* FROM etf_daily_data d1
    INNER JOIN (
        SELECT ticker, MAX(date) AS max_date FROM etf_daily_data
        WHERE current_price > 0 GROUP BY ticker
    ) d2 ON d1.ticker = d2.ticker AND d1.date = d2.max_date
) d ON p.ticker = d.ticker
"""


# ══════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════

@router.get("/api/portfolio")
async def get_portfolio(current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    usd_twd = get_usd_twd()

    with get_db() as (conn, cursor):
        # 持倉中部位（shares > 0）
        cursor.execute(f"""
            SELECT p.ticker, m.name, m.market,
                p.shares, p.avg_cost,
                COALESCE(p.realized_profit, 0) AS realized_profit,
                COALESCE(d.current_price, p.avg_cost) AS current_price,
                COALESCE(d.dividend_yield, 0) AS dividend_yield,
                COALESCE(d.payout_freq, '不配息') AS payout_freq,
                COALESCE(d.price_change_percent, 0) AS price_change_percent
            FROM user_portfolio p
            JOIN etf_master m ON p.ticker = m.ticker
            {LATEST_DAILY_JOIN}
            WHERE p.user_id=%s AND p.shares > 0
            ORDER BY (p.shares * COALESCE(d.current_price, p.avg_cost)) DESC
        """, (uid,))
        rows = cursor.fetchall()

        # 所有 ticker 的已實現損益合計（含已全部賣出的）
        cursor.execute(
            "SELECT COALESCE(SUM(realized_profit), 0) AS total_realized "
            "FROM user_portfolio WHERE user_id=%s",
            (uid,)
        )
        r_row = cursor.fetchone()
        total_realized_twd_raw = float((r_row or {}).get("total_realized") or 0)

    result, total_cost_twd, total_value_twd = [], 0.0, 0.0
    for r in rows:
        s    = float(r["shares"] or 0)
        ac   = float(r["avg_cost"] or 0)
        cp   = float(r["current_price"] or ac)
        rp   = float(r["realized_profit"] or 0)
        cost = s * ac
        val  = s * cp
        prof = val - cost          # 未實現損益（不含已實現）
        ret  = prof / cost * 100 if cost > 0 else 0.0

        fx = usd_twd if r["market"] == "US" else 1.0
        result.append({
            **r,
            "shares": s, "avg_cost": ac, "current_price": cp,
            "realized_profit": round(rp, 2),
            "cost":       round(cost, 2),
            "value":      round(val, 2),
            "profit":     round(prof, 2),    # 未實現損益
            "return_pct": round(ret, 2),
            "value_twd":  round(val * fx, 0),
            "cost_twd":   round(cost * fx, 0),
        })
        total_cost_twd  += cost * fx
        total_value_twd += val  * fx

    total_unrealized  = total_value_twd - total_cost_twd
    total_return      = total_unrealized / total_cost_twd * 100 if total_cost_twd > 0 else 0.0
    # 已實現損益目前以 TWD 儲存（TW 1:1，US 需換算；此處近似 — 歷史匯率未追蹤）
    total_realized_twd = round(total_realized_twd_raw, 0)

    return safe_json({
        "status": "success",
        "data": result,
        "summary": {
            "total_cost_twd":       round(total_cost_twd, 0),
            "total_value_twd":      round(total_value_twd, 0),
            "total_profit_twd":     round(total_unrealized, 0),   # 未實現
            "total_return":         round(total_return, 2),
            "total_realized_twd":   total_realized_twd,            # 已實現（含已賣光的）
            "usd_twd_rate":         usd_twd,
        }
    })


@router.post("/api/portfolio/transaction")
async def add_transaction(body: TransactionIn, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]

    # ── 第一層：cache 冪等（5s，攔截雙擊 / 網路重傳）──
    dedup_sig = hashlib.sha256(
        f"{uid}:{body.ticker}:{body.transaction_type}:{body.shares}:{body.price}:{body.transaction_date}".encode()
    ).hexdigest()[:16]
    dedup_key = f"txn_dedup:{dedup_sig}"
    if cache.get(dedup_key):
        return safe_json({"status": "error", "message": "重複提交：相同交易已在 5 秒內新增，請勿重複送出"}, 429)
    cache.set(dedup_key, 1, _DEDUP_TTL)

    # ── 第二層：DB 冪等（idempotency_key UNIQUE，跨重啟永久有效）──
    idem_key = body.idempotency_key or dedup_sig  # 前端送 UUID；未送則用 hash

    try:
        _insert_transaction(uid, body.dict(), idem_key)
        return safe_json({"status": "success", "message": "交易已新增"})
    except ValueError as e:
        cache.delete(dedup_key)   # 業務錯誤（庫存不足等）解除 cache 鎖，讓使用者更正後重送
        return safe_json({"status": "error", "message": str(e)}, 400)
    except Exception as ex:
        cache.delete(dedup_key)
        logger.error(f"add_transaction uid={uid}: {ex}", exc_info=True)
        return safe_json({"status": "error", "message": str(ex)}, 500)


@router.get("/api/portfolio/transactions")
async def get_transactions(
    ticker: str = None,
    limit: int = 100,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
):
    uid = current_user["id"]
    limit  = max(1, min(limit, 500))   # 上限 500，防止一次傾倒過多資料
    offset = max(0, offset)
    with get_db() as (conn, cursor):
        if ticker:
            cursor.execute(
                "SELECT id, ticker, transaction_type, shares, price, commission, "
                "transaction_date, note, created_at "
                "FROM user_transactions WHERE user_id=%s AND ticker=%s "
                "ORDER BY transaction_date DESC, id DESC LIMIT %s OFFSET %s",
                (uid, ticker.upper(), limit, offset)
            )
        else:
            cursor.execute(
                "SELECT id, ticker, transaction_type, shares, price, commission, "
                "transaction_date, note, created_at "
                "FROM user_transactions WHERE user_id=%s "
                "ORDER BY transaction_date DESC, id DESC LIMIT %s OFFSET %s",
                (uid, limit, offset)
            )
        rows = cursor.fetchall()
    return safe_json({"status": "success", "data": rows, "limit": limit, "offset": offset})


@router.delete("/api/portfolio/transaction/{tid}")
async def delete_transaction(tid: int, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    with get_db() as (conn, cursor):
        cursor.execute(
            "SELECT ticker FROM user_transactions WHERE id=%s AND user_id=%s", (tid, uid)
        )
        row = cursor.fetchone()
        if not row:
            return safe_json({"status": "error", "message": "找不到此交易"}, 404)
        ticker = row["ticker"]
        cursor.execute(
            "DELETE FROM user_transactions WHERE id=%s AND user_id=%s", (tid, uid)
        )
        _recalc_portfolio_cursor(uid, ticker, cursor)
        conn.commit()
    return safe_json({"status": "success", "message": "已刪除"})


# ══════════════════════════════════════════════════════════
#  私有邏輯
# ══════════════════════════════════════════════════════════

def _recalc_portfolio_cursor(uid: int, ticker: str, cursor):
    """從 user_transactions 重算持倉，並將結果（含已實現損益）寫回 user_portfolio。

    成本基礎算法：
      ① 買入：total_cost += shares × price + buy_commission
      ② 賣出：
         · 已實現損益 += (sell_price − avg_cost) × sell_shares − sell_commission
         · 從成本基礎扣除：total_cost −= avg_cost × sell_shares
         （賣出手續費 → 損益，不影響剩餘持倉的成本基礎）

    倉位歸零後保留 user_portfolio 紀錄（shares=0）以保存已實現損益歷史。
    """
    cursor.execute(
        "SELECT transaction_type, shares, price, commission FROM user_transactions "
        "WHERE user_id=%s AND ticker=%s ORDER BY transaction_date ASC, id ASC",
        (uid, ticker)
    )
    txs = cursor.fetchall()

    total_shares   = 0.0
    total_cost     = 0.0
    total_realized = 0.0

    for t in txs:
        s = float(t["shares"])
        p = float(t["price"])
        c = float(t.get("commission") or 0)

        if t["transaction_type"] == "buy":
            total_cost   += s * p + c   # 買入成本含手續費
            total_shares += s

        elif t["transaction_type"] == "sell":
            if total_shares > 0:
                avg_at_sell    = total_cost / total_shares
                # 已實現損益 = 價差 × 股數 − 賣出手續費
                total_realized += (p - avg_at_sell) * s - c
                total_cost     -= avg_at_sell * s          # 扣除成本基礎
            total_shares -= s
            if total_shares < 0 or abs(total_shares) < 1e-6:
                total_shares = 0.0
                total_cost   = 0.0

    avg_cost = total_cost / total_shares if total_shares > 0 else 0.0

    # 不論是否歸零，一律 UPSERT（保留 realized_profit 歷史）
    cursor.execute(
        "INSERT INTO user_portfolio "
        "(user_id, ticker, shares, avg_cost, realized_profit) "
        "VALUES (%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE "
        "shares=%s, avg_cost=%s, realized_profit=%s",
        (uid, ticker, total_shares, avg_cost, round(total_realized, 4),
         total_shares, avg_cost, round(total_realized, 4))
    )


def _insert_transaction(uid: int, data: dict, idem_key: str):
    """新增交易並在同一 DB transaction 內重算持倉。

    idem_key: 冪等鍵，存入 idempotency_key 欄位（UNIQUE）。
    若相同 key 已存在，DB 拋 Duplicate Entry → 上層回傳 429。
    """
    ticker  = data["ticker"].upper()
    tx_type = data["transaction_type"]
    shares  = float(data["shares"])
    price   = float(data["price"])
    comm    = float(data.get("commission") or 0)
    tx_date = data["transaction_date"]
    note    = data.get("note") or ""

    with get_db() as (conn, cursor):
        # 確保 ETF master 存在（自動探索模式）
        cursor.execute("SELECT ticker FROM etf_master WHERE ticker=%s", (ticker,))
        if not cursor.fetchone():
            market = "TW" if ticker[:4].isdigit() else "US"
            cursor.execute(
                "INSERT IGNORE INTO etf_master (ticker, name, market, auto_discovered) "
                "VALUES (%s,%s,%s,1)",
                (ticker, ticker, market)
            )

        # 賣出前檢查庫存（浮點容差 1e-6）
        if tx_type == "sell":
            cursor.execute(
                "SELECT shares FROM user_portfolio WHERE user_id=%s AND ticker=%s",
                (uid, ticker)
            )
            row  = cursor.fetchone()
            held = float(row["shares"]) if row else 0.0
            if held < shares - 1e-6:
                raise ValueError(f"庫存不足：持有 {held:.4f}，欲賣 {shares:.4f}")

        # INSERT — idempotency_key UNIQUE 索引是第二層防護
        # 若 DB 拋 Duplicate Entry（重複提交），由外層 except 捕獲並回傳 429
        try:
            cursor.execute(
                "INSERT INTO user_transactions "
                "(user_id, ticker, transaction_type, shares, price, commission, "
                "transaction_date, note, idempotency_key) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (uid, ticker, tx_type, shares, price, comm, tx_date, note, idem_key)
            )
        except Exception as e:
            err = str(e).lower()
            if "duplicate" in err or "unique" in err:
                raise ValueError("此交易已存在，請勿重複送出")
            raise

        # 重算在同一 DB transaction 內執行（不可拆非同步）
        _recalc_portfolio_cursor(uid, ticker, cursor)
        conn.commit()
