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
    _MAX_ALERTS = 50
    with get_db() as (conn, cursor):
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM price_alerts WHERE user_id=%s AND is_active=1", (uid,)
        )
        cnt = (cursor.fetchone() or {}).get("cnt", 0)
        if cnt >= _MAX_ALERTS:
            return safe_json(
                {"status": "error", "message": f"到價提醒最多設定 {_MAX_ALERTS} 筆，請先刪除舊的再新增"},
                400,
            )
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


# check_price_alerts 已移至 services/alerts.py（修正架構倒置：基礎設施層不應依賴展示層）
# 此處保留 re-export 以避免任何尚未更新的呼叫點出現 ImportError
from services.alerts import check_price_alerts  # noqa: E402, F401
