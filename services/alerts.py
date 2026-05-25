"""
services/alerts.py — 低檔加碼提醒 & 金字塔加碼法計算
"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
#  低檔加碼觸發判斷
# ══════════════════════════════════════════════════════════

def check_dip_alert(
    price_history: list[float],
    current_price: float,
    threshold_20d: float = 10.0,
    threshold_60d: float = 15.0,
) -> Optional[dict]:
    """
    price_history: 最近 60+ 個交易日的收盤價（由舊到新）
    current_price: 最新收盤價
    回傳 dict 若觸發提醒，否則回傳 None
    """
    if not price_history or current_price <= 0:
        return None

    prices = list(price_history) + [current_price]

    triggered = []

    # 20 個交易日跌幅
    if len(prices) >= 21:
        ref_20 = prices[-21]
        if ref_20 > 0:
            drop_20 = (ref_20 - current_price) / ref_20 * 100
            if drop_20 >= threshold_20d:
                triggered.append({
                    "period": "20日",
                    "drop_pct": round(drop_20, 2),
                    "threshold": threshold_20d,
                    "ref_price": round(ref_20, 2),
                })

    # 60 個交易日跌幅
    if len(prices) >= 61:
        ref_60 = prices[-61]
        if ref_60 > 0:
            drop_60 = (ref_60 - current_price) / ref_60 * 100
            if drop_60 >= threshold_60d:
                triggered.append({
                    "period": "60日",
                    "drop_pct": round(drop_60, 2),
                    "threshold": threshold_60d,
                    "ref_price": round(ref_60, 2),
                })

    if not triggered:
        return None

    # 取最大跌幅作為主要觸發
    main = max(triggered, key=lambda x: x["drop_pct"])
    return {
        "triggered": True,
        "triggers": triggered,
        "main_drop_pct": main["drop_pct"],
        "main_period": main["period"],
        "current_price": current_price,
        "recommendation": _build_dip_recommendation(main["drop_pct"]),
    }


def _build_dip_recommendation(drop_pct: float) -> dict:
    """根據跌幅給出金字塔加碼建議"""
    if drop_pct >= 30:
        extra_pct = 100
        level = "重大回撤"
        reason = f"下跌 {drop_pct:.1f}%，已接近歷史重大回撤水準，建議大幅加碼。"
    elif drop_pct >= 20:
        extra_pct = 75
        level = "深度修正"
        reason = f"下跌 {drop_pct:.1f}%，進入深度修正區間，建議積極加碼。"
    elif drop_pct >= 15:
        extra_pct = 50
        level = "中度修正"
        reason = f"下跌 {drop_pct:.1f}%，達中度修正標準，建議加碼以降低均價。"
    elif drop_pct >= 10:
        extra_pct = 25
        level = "輕度回調"
        reason = f"下跌 {drop_pct:.1f}%，達輕度回調標準，可小幅加碼。"
    else:
        extra_pct = 10
        level = "觀察"
        reason = f"下跌 {drop_pct:.1f}%，尚未達到加碼門檻，建議觀察。"

    return {
        "level": level,
        "reason": reason,
        "extra_pct": extra_pct,
        "description": (
            f"建議在本月定期定額之外，額外加碼 {extra_pct}%。"
            f"例如每月固定投入 10,000 元，本月建議總投入 {10000 * (1 + extra_pct/100):,.0f} 元。"
        )
    }


# ══════════════════════════════════════════════════════════
#  金字塔加碼法（回測中使用）
# ══════════════════════════════════════════════════════════

def pyramid_extra_amount(
    monthly_amount: float,
    price_history_window: list[float],
    current_price: float,
    dip_threshold_20d: float = 10.0,
    dip_threshold_60d: float = 15.0,
    dip_extra_pct: float = 50.0,
) -> float:
    """
    計算金字塔加碼的額外投入金額。
    回傳 extra_amount (>=0)，應加在本月 monthly_amount 之上。
    """
    alert = check_dip_alert(
        price_history_window, current_price,
        dip_threshold_20d, dip_threshold_60d
    )
    if not alert:
        return 0.0

    drop = alert["main_drop_pct"]
    # 梯階式加碼
    if drop >= 30:
        ratio = 1.0
    elif drop >= 20:
        ratio = 0.75
    elif drop >= 15:
        ratio = dip_extra_pct / 100
    elif drop >= 10:
        ratio = dip_extra_pct / 100 * 0.5
    else:
        ratio = 0.0

    return round(monthly_amount * ratio, 2)


# ══════════════════════════════════════════════════════════
#  到價提醒掃描（原本住在 routes/notification_routes.py）
#
#  設計原則：純業務邏輯應住在 services/，不得依賴展示層（routes）。
#  scheduler.py 需要此函式，若放在 routes 則形成「基礎設施層 → 展示層」的
#  反向依賴（Dependency Inversion 違反）。
# ══════════════════════════════════════════════════════════

def check_price_alerts(ticker: str, current_price: float) -> None:
    """掃描 price_alerts 表，對到價的 alert 推送通知並標記已觸發。

    單一 DB 連線完成讀取、批次插入通知、批次更新狀態，
    避免 N×3 次連線開銷。由排程器（_fast_price_tick / _update_active）呼叫。
    """
    import json
    from database import get_db

    try:
        with get_db() as (conn, cursor):
            cursor.execute(
                "SELECT pa.*, m.name FROM price_alerts pa "
                "LEFT JOIN etf_master m ON pa.ticker=m.ticker "
                "WHERE pa.ticker=%s AND pa.is_active=1 AND pa.is_triggered=0",
                (ticker,),
            )
            alerts = cursor.fetchall()

            triggered_ids = []
            for alert in alerts:
                hit = (
                    (alert["alert_type"] == "above" and current_price >= float(alert["target_price"]))
                    or (alert["alert_type"] == "below" and current_price <= float(alert["target_price"]))
                )
                if not hit:
                    continue

                direction = "突破" if alert["alert_type"] == "above" else "跌破"
                title   = f"📈 {alert.get('name', ticker)} ({ticker}) {direction}目標價"
                content = (
                    f"{alert.get('name', ticker)} 目前價格 {current_price}，"
                    f"已{direction}您設定的目標價 {float(alert['target_price'])}。"
                )
                cursor.execute(
                    "INSERT INTO notifications (user_id,type,title,content,ticker) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (alert["user_id"], "price_alert", title, content, ticker),
                )
                triggered_ids.append(alert["id"])

            if triggered_ids:
                placeholders = ",".join(["%s"] * len(triggered_ids))
                cursor.execute(
                    f"UPDATE price_alerts SET is_triggered=1 WHERE id IN ({placeholders})",
                    triggered_ids,
                )
                conn.commit()
    except Exception as e:
        logger.warning(f"check_price_alerts({ticker}): {e}")


# ══════════════════════════════════════════════════════════
#  批次產生警示（排程器呼叫）
# ══════════════════════════════════════════════════════════

def generate_dip_notifications(ticker: str, etf_name: str, price_history: list[float], current_price: float) -> list[dict]:
    """產生通知資料（存入 DB 前的 dict 清單）"""
    alert = check_dip_alert(price_history, current_price)
    if not alert:
        return []

    notes = []
    for t in alert["triggers"]:
        rec = _build_dip_recommendation(t["drop_pct"])
        notes.append({
            "type": "dip_alert",
            "title": f"⚠️ {etf_name} ({ticker}) 低檔加碼提醒",
            "content": (
                f"{etf_name} 最近 {t['period']} 下跌 {t['drop_pct']}%，"
                f"由 {t['ref_price']} 跌至 {current_price}。\n"
                f"【{rec['level']}】{rec['reason']}\n"
                f"{rec['description']}"
            ),
            "ticker": ticker,
        })
    return notes
