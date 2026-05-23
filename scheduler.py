"""
scheduler.py — APScheduler 排程器

排程項目（Asia/Taipei 時區）：
  07:00  _update_missing()  — 補漏掃描：熱門 ETF 中超過 3 天無資料者，靜默補抓
  08:00  sync_tw_etfs()     — 同步 TWSE/TPEX 全市場台股 ETF 代碼
  09:10  _update_active()   — 台股開盤後第一次更新
  12:00  _update_active()   — 台股盤中
  14:35  _update_active()   — 台股收盤後
  22:00  _update_active()   — 美股開盤後 30 分（21:30 開盤）
  00:30  _update_active()   — 美股盤中（美東 11:30）
  每 30 分 _evict_cache     — 清除過期快取
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


# ──────────────────────────────────────────────
#  排程橋接（把協程安全地丟進主事件迴圈）
# ──────────────────────────────────────────────

def schedule_update():
    if MAIN_LOOP and MAIN_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(_update_active(), MAIN_LOOP)


def schedule_missing():
    if MAIN_LOOP and MAIN_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(_update_missing(), MAIN_LOOP)


def schedule_twse_sync():
    if MAIN_LOOP and MAIN_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(_run_twse_sync(), MAIN_LOOP)


# ──────────────────────────────────────────────
#  活躍代碼池：庫存 + 自選股 + 熱門標的
# ──────────────────────────────────────────────

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


# ──────────────────────────────────────────────
#  主更新邏輯（盤中高頻更新用）
# ──────────────────────────────────────────────

async def _update_active():
    """只更新「有人在意」的 ETF：庫存 + 自選股 + 熱門。"""
    from etf_data import fetch_one_etf, save_etf_data

    pool = _get_active_pool()
    if not pool:
        logger.info("★ 排程更新：活躍池為空，略過")
        return

    logger.info(f"★ 排程更新：{len(pool)} 檔活躍 ETF")

    # 並發數 3，避免 Yahoo Finance 429 限速（每 ETF 需發 4-6 次請求）
    BATCH = 3
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
            result["market"] = etf["market"]
            ticker        = etf["ticker"]
            current_price = float(result.get("current_price", 0))
            try:
                save_etf_data(result)
                logger.info(f"  ✓ {ticker} {current_price}")
            except Exception as e:
                logger.warning(f"  save {ticker} 失敗: {e}")
                continue
            try:
                from routes.notification_routes import check_price_alerts
                check_price_alerts(ticker, current_price)
            except Exception as e:
                logger.debug(f"  price alert {ticker}: {e}")

        await asyncio.sleep(random.uniform(5, 9))

    logger.info("✅ 排程更新完成")


# ──────────────────────────────────────────────
#  補漏掃描（每日 07:00，低頻保守抓取）
# ──────────────────────────────────────────────

async def _update_missing():
    """找出熱門 ETF 清單中「超過 3 天沒有資料」的標的，靜默補抓。

    解決的問題：
    - 排行榜出現空白 / 「數據抓取中」：hot ETF 從未被抓過
    - Railway 重啟後、或某次排程失敗後，少數標的資料斷層
    使用保守批次（2 檔 / 批）和較長等待（15-20 秒），避免凌晨觸發 Yahoo 429。
    """
    from etf_data import fetch_one_etf, save_etf_data
    from database import get_db

    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT m.ticker, m.market
                FROM etf_master m
                LEFT JOIN (
                    SELECT ticker, MAX(date) AS last_date
                    FROM etf_daily_data
                    GROUP BY ticker
                ) d ON m.ticker = d.ticker
                WHERE m.is_hot = 1
                  AND (
                      d.last_date IS NULL
                      OR d.last_date < DATE_SUB(CURDATE(), INTERVAL 3 DAY)
                  )
                ORDER BY d.last_date ASC NULLS FIRST
                LIMIT 30
            """)
            rows = cursor.fetchall()
    except Exception:
        # TiDB / MySQL 不支援 NULLS FIRST，fallback 寫法
        try:
            with get_db() as (conn, cursor):
                cursor.execute("""
                    SELECT m.ticker, m.market
                    FROM etf_master m
                    LEFT JOIN (
                        SELECT ticker, MAX(date) AS last_date
                        FROM etf_daily_data
                        GROUP BY ticker
                    ) d ON m.ticker = d.ticker
                    WHERE m.is_hot = 1
                      AND (
                          d.last_date IS NULL
                          OR d.last_date < DATE_SUB(CURDATE(), INTERVAL 3 DAY)
                      )
                    ORDER BY ISNULL(d.last_date) DESC, d.last_date ASC
                    LIMIT 30
                """)
                rows = cursor.fetchall()
        except Exception as e:
            logger.warning(f"_update_missing 查詢失敗: {e}")
            return

    if not rows:
        logger.info("★ 補漏掃描：所有熱門 ETF 資料均已是近期，略過")
        return

    logger.info(f"★ 補漏掃描：找到 {len(rows)} 檔待補抓（超過 3 天未更新）")

    BATCH = 2  # 保守並發，降低被 Yahoo 封鎖風險
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        results = await asyncio.gather(
            *[asyncio.to_thread(fetch_one_etf, e["ticker"], e["market"]) for e in batch],
            return_exceptions=True,
        )
        for etf, result in zip(batch, results):
            if isinstance(result, Exception) or not result:
                if isinstance(result, Exception):
                    logger.warning(f"  ✗ (補漏) {etf['ticker']}: {result}")
                continue
            result["ticker"] = etf["ticker"]
            result["market"] = etf["market"]
            try:
                save_etf_data(result)
                logger.info(f"  ✓ (補漏) {etf['ticker']} {result.get('current_price', 0)}")
            except Exception as e:
                logger.warning(f"  save (補漏) {etf['ticker']}: {e}")

        await asyncio.sleep(random.uniform(15, 20))  # 保守等待，降低 429 風險

    logger.info("✅ 補漏掃描完成")


# ──────────────────────────────────────────────
#  TWSE 全市場同步
# ──────────────────────────────────────────────

async def _run_twse_sync():
    from services.twse_sync import sync_tw_etfs
    try:
        new_count = await asyncio.to_thread(sync_tw_etfs)
        logger.info(f"✅ TWSE 排程同步完成，新增 {new_count} 檔")
    except Exception as e:
        logger.warning(f"TWSE 同步失敗: {e}")


# ──────────────────────────────────────────────
#  價格警報檢查
# ──────────────────────────────────────────────

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


# ──────────────────────────────────────────────
#  快取清理
# ──────────────────────────────────────────────

def _evict_cache():
    from cache import cache
    cache.evict()


# ──────────────────────────────────────────────
#  啟動排程器
# ──────────────────────────────────────────────

def start_scheduler() -> BackgroundScheduler:
    sch = BackgroundScheduler(timezone="Asia/Taipei")

    # ── 補漏掃描（07:00，兩市場均未開盤，保守抓取）──
    sch.add_job(lambda: schedule_missing(),   CronTrigger(hour=7,  minute=0),  max_instances=1)

    # ── TWSE 全市場代碼同步 ──
    sch.add_job(lambda: schedule_twse_sync(), CronTrigger(hour=8,  minute=0),  max_instances=1)

    # ── 台股盤中（09:10 / 12:00 / 14:35）──
    sch.add_job(lambda: schedule_update(),    CronTrigger(hour=9,  minute=10), max_instances=1)
    sch.add_job(lambda: schedule_update(),    CronTrigger(hour=12, minute=0),  max_instances=1)
    sch.add_job(lambda: schedule_update(),    CronTrigger(hour=14, minute=35), max_instances=1)

    # ── 美股盤中（22:00 開盤後 / 00:30 盤中）──
    sch.add_job(lambda: schedule_update(),    CronTrigger(hour=22, minute=0),  max_instances=1)
    sch.add_job(lambda: schedule_update(),    CronTrigger(hour=0,  minute=30), max_instances=1)

    # ── 快取維護（每 30 分鐘）──
    sch.add_job(_evict_cache, "interval", minutes=30, max_instances=1)

    sch.start()
    logger.info(
        "✅ 排程器已啟動\n"
        "   07:00 補漏掃描 | 08:00 TWSE 同步\n"
        "   09:10 / 12:00 / 14:35 台股盤中\n"
        "   22:00 / 00:30 美股盤中 | 每30分清快取"
    )
    return sch
