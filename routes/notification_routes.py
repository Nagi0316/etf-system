"""
routes/notification_routes.py — 通知中心 & 到價提醒管理
"""
import json, logging
from fastapi import APIRouter, Depends

from auth import get_current_user
from models import PriceAlertIn
from database import get_db
from utils import safe_json

logger = logging.getLogger(__name__)
router = APIRouter()


# ══════════════════════════════════════════════════════════
#  通知
# ══════════════════════════════════════════════════════════

@router.get("/api/notifications")
async def get_notifications(
    unread_only: bool = False,
    page: int = 1,
    current_user: dict = Depends(get_current_user),
):
    uid = current_user["id"]
    limit  = 20
    offset = (page - 1) * limit
    with get_db() as (conn, cursor):
        cond = "AND is_read=0" if unread_only else ""
        cursor.execute(
            f"SELECT * FROM notifications WHERE user_id=%s {cond} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (uid, limit, offset)
        )
        rows = cursor.fetchall()
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM notifications WHERE user_id=%s AND is_read=0",
            (uid,)
        )
        unread_count = (cursor.fetchone() or {}).get("cnt", 0)

    # 解析 extra_data
    for r in rows:
        if r.get("extra_data"):
            try:
                r["extra_data"] = json.loads(r["extra_data"])
            except Exception:
                pass

    return safe_json({"status": "success", "data": rows, "unread_count": unread_count})


@router.post("/api/notifications/{nid}/read")
async def mark_read(nid: int, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    with get_db() as (conn, cursor):
        cursor.execute("UPDATE notifications SET is_read=1 WHERE id=%s AND user_id=%s", (nid, uid))
        conn.commit()
    return safe_json({"status": "success"})


@router.post("/api/notifications/read-all")
async def mark_all_read(current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    with get_db() as (conn, cursor):
        cursor.execute("UPDATE notifications SET is_read=1 WHERE user_id=%s", (uid,))
        conn.commit()
    return safe_json({"status": "success"})


@router.delete("/api/notifications/{nid}")
async def delete_notification(nid: int, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    with get_db() as (conn, cursor):
        cursor.execute("DELETE FROM notifications WHERE id=%s AND user_id=%s", (nid, uid))
        conn.commit()
    return safe_json({"status": "success"})


# ══════════════════════════════════════════════════════════
#  到價提醒 (Price Alerts)
# ══════════════════════════════════════════════════════════

@router.get("/api/price-alerts")
async def get_price_alerts(current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    with get_db() as (conn, cursor):
        cursor.execute(
            "SELECT pa.*, m.name FROM price_alerts pa "
            "LEFT JOIN etf_master m ON pa.ticker=m.ticker "
            "WHERE pa.user_id=%s AND pa.is_active=1 ORDER BY pa.created_at DESC",
            (uid,)
        )
        rows = cursor.fetchall()
    return safe_json({"status": "success", "data": rows})


@router.post("/api/price-alerts")
async def create_price_alert(body: PriceAlertIn, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    with get_db() as (conn, cursor):
        cursor.execute(
            "INSERT INTO price_alerts (user_id,ticker,alert_type,target_price) VALUES (%s,%s,%s,%s)",
            (uid, body.ticker, body.alert_type, body.target_price)
        )
        conn.commit()
    return safe_json({"status": "success", "message": "到價提醒已設定"})


@router.delete("/api/price-alerts/{aid}")
async def delete_price_alert(aid: int, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    with get_db() as (conn, cursor):
        cursor.execute("UPDATE price_alerts SET is_active=0 WHERE id=%s AND user_id=%s", (aid, uid))
        conn.commit()
    return safe_json({"status": "success"})


# ── 建立系統通知（供排程器呼叫）──

def push_notification(user_id: int, ntype: str, title: str, content: str, ticker: str = None, extra: dict = None):
    extra_str = json.dumps(extra) if extra else None
    with get_db() as (conn, cursor):
        cursor.execute(
            "INSERT INTO notifications (user_id,type,title,content,ticker,extra_data) VALUES (%s,%s,%s,%s,%s,%s)",
            (user_id, ntype, title, content, ticker, extra_str)
        )
        conn.commit()


def check_price_alerts(ticker: str, current_price: float):
    """排程器呼叫：檢查到價提醒並推送通知"""
    with get_db() as (conn, cursor):
        cursor.execute(
            "SELECT pa.*, m.name FROM price_alerts pa "
            "LEFT JOIN etf_master m ON pa.ticker=m.ticker "
            "WHERE pa.ticker=%s AND pa.is_active=1 AND pa.is_triggered=0",
            (ticker,)
        )
        alerts = cursor.fetchall()

    for alert in alerts:
        triggered = False
        if alert["alert_type"] == "above" and current_price >= float(alert["target_price"]):
            triggered = True
        elif alert["alert_type"] == "below" and current_price <= float(alert["target_price"]):
            triggered = True

        if triggered:
            direction = "突破" if alert["alert_type"] == "above" else "跌破"
            title   = f"📈 {alert.get('name', ticker)} ({ticker}) {direction}目標價"
            content = (
                f"{alert.get('name', ticker)} 目前價格 {current_price}，"
                f"已{direction}您設定的目標價 {float(alert['target_price'])}。"
            )
            push_notification(alert["user_id"], "price_alert", title, content, ticker)
            with get_db() as (conn, cursor):
                cursor.execute("UPDATE price_alerts SET is_triggered=1 WHERE id=%s", (alert["id"],))
                conn.commit()
