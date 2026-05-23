"""
scheduler.py — APScheduler 排程器

排程架構（Asia/Taipei 時區）：

【盤中快速】每 2 分鐘，內部偵測市場是否開盤
  ▸ 只抓現價：TW 走 TWSE 官方 API，US 走輕量 Yahoo chart
  ▸ 更新完立即掃描到價提醒（延遲 ≤ 2 分鐘）
  ▸ 不碰 dividend / history / quoteSummary，Yahoo 請求量降 ~70%

【盤中完整】每 30 分鐘，內部偵測市場是否開盤
  ▸ 完整抓取含 dividend、5 年歷史月線、費用率、NAV 等補充資料

【固定排程】
  07:00  _update_missing()  — 補漏掃描（熱門 ETF 超過 3 天未更新）
  08:00  sync_tw_etfs()     — 同步 TWSE/TPEX 全市場代碼
  14:35  _update_active()   — 台股收盤後確認收盤價
  04:15  _update_active()   — 美股收盤後確認收盤價
  每 30 分 _evict_cache     — 清除過期快取
"""
import asyncio
import logging
import random
from datetime import time as _time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
MAIN_LOOP = None

_TZ_TAIPEI = ZoneInfo("Asia/Taipei")
_TZ_NY     = ZoneInfo("America/New_York")  # DST-aware（夏令 UTC-4 / 冬令 UTC-5）


def _is_tw_market_open() -> bool:
    """台股 09:00–13:30，週一至週五。"""
    from datetime import datetime
    now = datetime.now(_TZ_TAIPEI)
    if now.weekday() >= 5:
        return False
    return _time(9, 0) <= now.time() <= _time(13, 30)


def _is_us_market_open() -> bool:
    """美股 09:30–16:00（紐約時間，自動處理 DST）。"""
    from datetime import datetime
    now_ny = datetime.now(_TZ_NY)
    if now_ny.weekday() >= 5:
        return False
    return _time(9, 30) <= now_ny.time() <= _time(16, 0)


def _is_any_market_open() -> bool:
    return _is_tw_market_open() or _is_us_market_open()


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


def schedule_fast_price_tick():
    """盤中快速報價 tick：只更新現價 + 立即掃描到價提醒。僅開盤時執行。"""
    if not _is_any_market_open():
        return
    if MAIN_LOOP and MAIN_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(_fast_price_tick(), MAIN_LOOP)


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
#  盤中快速報價 tick（每 2 分鐘）
#  只抓現價，不碰 Yahoo 補充資料，降低 Yahoo 429 風險
# ──────────────────────────────────────────────

async def _fast_price_tick():
    """盤中高頻：只更新現價（TW 走 TWSE 官方 API，US 走輕量 Yahoo chart）。
    60 檔預估耗時 30-50 秒，遠低於原本的 140-280 秒。
    更新完成後立即掃描到價提醒，實現 2 分鐘以內的 alert 延遲。
    """
    tw_open = _is_tw_market_open()
    us_open = _is_us_market_open()
    if not (tw_open or us_open):
        return

    from etf_data import fetch_price_only, save_price_only

    pool = _get_active_pool()
    if not pool:
        return

    active_pool = [
        e for e in pool
        if (e["market"] == "TW" and tw_open) or (e["market"] == "US" and us_open)
    ]
    if not active_pool:
        return

    logger.debug(f"⚡ 快速報價 tick：{len(active_pool)} 檔（TW={'開' if tw_open else '收'} US={'開' if us_open else '收'}）")

    # TW 用 TWSE 官方 API，輕量可加大批次；US 仍走 Yahoo，保守並發
    tw_pool = [e for e in active_pool if e["market"] == "TW"]
    us_pool = [e for e in active_pool if e["market"] == "US"]

    updated_any = False

    async def _process_batch(batch, batch_size, sleep_range):
        nonlocal updated_any
        for i in range(0, len(batch), batch_size):
            chunk = batch[i:i + batch_size]
            results = await asyncio.gather(
                *[asyncio.to_thread(fetch_price_only, e["ticker"], e["market"]) for e in chunk],
                return_exceptions=True,
            )
            for etf, result in zip(chunk, results):
                if isinstance(result, Exception) or not result:
                    continue
                try:
                    save_price_only(result)
                    updated_any = True
                except Exception as e:
                    logger.debug(f"  fast save {etf['ticker']}: {e}")
            if i + batch_size < len(batch):
                await asyncio.sleep(random.uniform(*sleep_range))

    if tw_pool:
        await _process_batch(tw_pool, batch_size=6, sleep_range=(1.0, 2.0))
    if us_pool:
        await _process_batch(us_pool, batch_size=3, sleep_range=(3.0, 5.0))

    if updated_any:
        await _check_price_alerts()
        logger.debug("⚡ 快速報價 tick 完成，到價提醒已掃描")


# ──────────────────────────────────────────────
#  盤中完整資料 tick（每 30 分鐘）：含補充資料
# ──────────────────────────────────────────────

async def _market_tick():
    """盤中完整資料更新（每 30 分鐘）：
    抓取完整 ETF 資料含 dividend、歷史月線、費用率等補充資料。
    現價更新由 _fast_price_tick（每 2 分鐘）負責，此函式不重複掃描 alert。
    """
    tw_open = _is_tw_market_open()
    us_open = _is_us_market_open()
    if not (tw_open or us_open):
        return  # 雙市場均已收盤，本 tick 跳過

    from etf_data import fetch_one_etf, save_etf_data

    pool = _get_active_pool()
    if not pool:
        return

    # 依市場篩選：只更新當前開盤市場的 ETF，降低 Yahoo Finance 請求量
    active_market_pool = [
        e for e in pool
        if (e["market"] == "TW" and tw_open) or (e["market"] == "US" and us_open)
    ]
    if not active_market_pool:
        return

    logger.debug(f"⏱ 盤中 tick：更新 {len(active_market_pool)} 檔（TW={'開' if tw_open else '收'} US={'開' if us_open else '收'}）")

    BATCH = 3
    updated_any = False
    for i in range(0, len(active_market_pool), BATCH):
        batch = active_market_pool[i:i + BATCH]
        results = await asyncio.gather(
            *[asyncio.to_thread(fetch_one_etf, e["ticker"], e["market"]) for e in batch],
            return_exceptions=True,
        )
        for etf, result in zip(batch, results):
            if isinstance(result, Exception) or not result:
                continue
            result["ticker"] = etf["ticker"]
            result["market"]  = etf["market"]
            try:
                save_etf_data(result)
                updated_any = True
            except Exception as e:
                logger.debug(f"  tick save {etf['ticker']}: {e}")
        await asyncio.sleep(random.uniform(3, 5))

    if updated_any:
        logger.debug("⏱ 盤中完整更新完成")


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

    # ════════════════════════════════════════
    #  盤中排程（interval，內部判斷是否開盤）
    # ════════════════════════════════════════

    # 每 2 分鐘：快速報價更新 + 到價提醒掃描（開盤期間）
    # 只走 TWSE 官方 API（TW）或輕量 Yahoo chart（US），不碰 dividend/history
    sch.add_job(
        lambda: schedule_fast_price_tick(),
        "interval", minutes=2,
        id="fast_price_tick", max_instances=1,
    )

    # 每 30 分鐘：完整資料更新含 dividend/history/費用率（開盤期間）
    sch.add_job(
        lambda: (
            _is_any_market_open() and
            MAIN_LOOP and MAIN_LOOP.is_running() and
            asyncio.run_coroutine_threadsafe(_market_tick(), MAIN_LOOP)
        ),
        "interval", minutes=30,
        id="full_data_tick", max_instances=1,
    )

    # ════════════════════════════════════════
    #  固定排程（保底 / 低頻維護）
    # ════════════════════════════════════════

    # 每日 07:00 補漏掃描（盤前）
    sch.add_job(lambda: schedule_missing(),   CronTrigger(hour=7,  minute=0),  max_instances=1)

    # 每日 08:00 同步 TWSE/TPEX 全市場代碼
    sch.add_job(lambda: schedule_twse_sync(), CronTrigger(hour=8,  minute=0),  max_instances=1)

    # 14:35 台股收盤後完整更新收盤資料
    sch.add_job(lambda: schedule_update(),    CronTrigger(hour=14, minute=35), max_instances=1)

    # 04:15 美股收盤後完整更新收盤資料（美東 16:15，夏令對應台灣 04:15）
    sch.add_job(lambda: schedule_update(),    CronTrigger(hour=4,  minute=15), max_instances=1)

    # 快取維護（每 30 分鐘）
    sch.add_job(_evict_cache, "interval", minutes=30, max_instances=1)

    sch.start()
    logger.info(
        "✅ 排程器已啟動\n"
        "   【盤中快速】每 2 分鐘更新現價 + 掃描到價提醒（開盤期間）\n"
        "   【盤中完整】每 30 分鐘更新含 dividend/history 補充資料\n"
        "   【台股】09:00–13:30 Asia/Taipei\n"
        "   【美股】09:30–16:00 America/New_York（DST-aware）\n"
        "   07:00 補漏掃描 | 08:00 TWSE 同步\n"
        "   14:35 台股收盤完整更新 | 04:15 美股收盤完整更新 | 每30分清快取"
    )
    return sch
