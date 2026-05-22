"""
scheduler.py — APScheduler 排程器

排程項目：
  08:00  sync_tw_etfs()      — 同步 TWSE/TPEX 全市場台股 ETF 代碼
  14:30  _update_active()    — 台股收盤後更新活躍標的
  21:00  _update_active()    — 美股開盤後更新活躍標的
"""
import asyncio
import logging
import random

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
MAIN_LOOP = None


def set_loop(loop):
    global MAIN_LOOP
    MAIN_LOOP = loop


def schedule_update():
    if MAIN_LOOP and MAIN_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(_update_active(), MAIN_LOOP)


def schedule_twse_sync():
    if MAIN_LOOP and MAIN_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(_run_twse_sync(), MAIN_LOOP)


# ── 取得「活躍代碼池」：用戶庫存 + 自選股 + 熱門標的 ──

def _get_active_pool() -> list[dict]:
    """從資料庫動態建構需要更新的 ETF 清單，不依賴任何寫死清單。"""
    from database import get_db
    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT DISTINCT m.ticker, m.market
                FROM etf_master m
                WHERE m.is_hot = 1
                  OR m.ticker IN (
                      SELECT DISTINCT ticker FROM user_watchlist
                      UNION
                      SELECT DISTINCT ticker FROM user_portfolio WHERE shares > 0
                  )
                ORDER BY m.is_hot DESC, m.ticker
            """)
            rows = cursor.fetchall()
        return [{"ticker": r["ticker"], "market": r["market"]} for r in rows]
    except Exception as e:
        logger.warning(f"_get_active_pool 失敗: {e}，回退到靜態清單")
        from etf_data import HOT_ETFS
        return HOT_ETFS


# ── 主更新邏輯 ──

async def _update_active():
    """只更新「有人在意」的 ETF：庫存 + 自選股 + 熱門。"""
    from etf_data import fetch_one_etf, save_etf_data

    pool = _get_active_pool()
    if not pool:
        logger.info("★ 排程更新：活躍池為空，略過")
        return

    logger.info(f"★ 排程更新：{len(pool)} 檔活躍 ETF")

    BATCH = 5
    for i in range(0, len(pool), BATCH):
        batch = pool[i:i + BATCH]
        results = await asyncio.gather(
            *[asyncio.to_thread(fetch_one_etf, e["ticker"], e["market"]) for e in batch],
            return_exceptions=True,
        )
        for etf, result in zip(batch, results):
            if isinstance(result, Exception) or not result:
                if isinstance(result, Exception):
                    logger.warning(f"  ✗ {etf['ticker']}: {result}")
                continue
            result["ticker"] = etf["ticker"]
            try:
                save_etf_data(result)
            except Exception as e:
                logger.warning(f"  save {etf['ticker']} 失敗: {e}")

        await asyncio.sleep(random.uniform(3, 6))

    await _check_price_alerts()
    logger.info("✅ 排程更新完成")


async def _run_twse_sync():
    from services.twse_sync import sync_tw_etfs
    try:
        new_count = await asyncio.to_thread(sync_tw_etfs)
        logger.info(f"✅ TWSE 排程同步完成，新增 {new_count} 檔")
    except Exception as e:
        logger.warning(f"TWSE 同步失敗: {e}")


async def _check_price_alerts():
    from routes.notification_routes import check_price_alerts
    from database import get_db
    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT pa.ticker, d.current_price
                FROM price_alerts pa
                JOIN (
                    SELECT d1.ticker, d1.current_price FROM etf_daily_data d1
                    INNER JOIN (
                        SELECT ticker, MAX(date) as md FROM etf_daily_data GROUP BY ticker
                    ) d2 ON d1.ticker=d2.ticker AND d1.date=d2.md
                ) d ON pa.ticker=d.ticker
                WHERE pa.is_active=1 AND pa.is_triggered=0
                GROUP BY pa.ticker, d.current_price
            """)
            rows = cursor.fetchall()
        for row in rows:
            check_price_alerts(row["ticker"], float(row["current_price"]))
    except Exception as e:
        logger.warning(f"_check_price_alerts 失敗: {e}")


def start_scheduler() -> BackgroundScheduler:
    sch = BackgroundScheduler(timezone="Asia/Taipei")
    sch.add_job(lambda: schedule_twse_sync(), CronTrigger(hour=8,  minute=0))
    sch.add_job(lambda: schedule_update(),    CronTrigger(hour=14, minute=30))
    sch.add_job(lambda: schedule_update(),    CronTrigger(hour=21, minute=0))
    sch.start()
    logger.info("✅ 排程器已啟動（08:00 TWSE 同步 / 14:30 / 21:00 更新）")
    return sch
