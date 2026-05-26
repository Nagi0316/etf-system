"""
scheduler.py — APScheduler 排程器

排程架構（Asia/Taipei 時區）：

【盤中快速】每 30 秒，內部偵測市場是否開盤
  ▸ 批量抓現價：TW 走 TWSE MIS 批量 API（全部 1-2 次 HTTP）
               US 走 Yahoo /v7/finance/quote?symbols=（全部 1 次 HTTP）
  ▸ dirty check：price / volume 均未變動的 ETF 跳過 DB 寫入
  ▸ executemany 批次寫入 → 1 次 DB 往返（TiDB 150ms RTT × 1）
  ▸ 更新完立即掃描到價提醒（延遲 ≤ 30 秒）
  ▸ 不碰 dividend / history / quoteSummary，Yahoo 請求量降 ~70%

【盤中完整】每 30 分鐘，內部偵測市場是否開盤
  ▸ 完整抓取含 dividend、5 年歷史月線、費用率、NAV 等補充資料

【固定排程】
  07:00  _update_missing()  — 補漏掃描（熱門 ETF 超過 3 天未更新）
  08:00  sync_tw_etfs()     — 同步 TWSE/TPEX 全市場代碼
  14:35  _update_active()   — 台股收盤後確認收盤價
  05:15  _update_active()   — 美股收盤後確認收盤價（EDT/EST 皆正確）
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
                SELECT DISTINCT m.ticker, m.market, m.is_hot
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
                from services.alerts import check_price_alerts
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
    try:
      await _fast_price_tick_inner()
    except Exception as e:
        logger.error(f"_fast_price_tick 未預期例外（排程器仍繼續運行）: {e}", exc_info=True)


async def _fast_price_tick_inner():
    """盤中高頻批量報價更新。

    優化架構（vs 舊版逐筆 fetch）：
      · TW：_fetch_tw_realtime_bulk → TWSE MIS 批量 API，所有台股 1-2 次 HTTP
      · US：_fetch_us_realtime_bulk → Yahoo v7/quote?symbols=，所有美股 1 次 HTTP
      · save_price_bulk → executemany 一次 DB 往返 + dirty check 跳過未變動標的
                          + cache.delete_prefix("rank:") 整批只呼叫 1 次
      預估 60 檔從 60+ 秒降至 2-3 秒（含 TiDB 150ms RTT）。
    """
    tw_open = _is_tw_market_open()
    us_open = _is_us_market_open()
    if not (tw_open or us_open):
        return

    from etf_data import _fetch_tw_realtime_bulk, _fetch_us_realtime_bulk, save_price_bulk

    pool = _get_active_pool()
    if not pool:
        return

    tw_pool = [e for e in pool if e["market"] == "TW" and tw_open]
    us_pool = [e for e in pool if e["market"] == "US" and us_open]

    if not (tw_pool or us_pool):
        return

    logger.debug(
        f"⚡ 批量快速報價 tick：TW={len(tw_pool)} 檔，US={len(us_pool)} 檔"
        f"（TW={'開' if tw_open else '收'} US={'開' if us_open else '收'}）"
    )

    # ── 並行發起 TW / US 批量請求（兩個 to_thread 同時跑，互不阻塞）──
    tw_tickers = [e["ticker"] for e in tw_pool]
    us_tickers = [e["ticker"] for e in us_pool]

    tw_raw: dict = {}
    us_raw: dict = {}

    fetch_coros = []
    if tw_tickers:
        fetch_coros.append(asyncio.to_thread(_fetch_tw_realtime_bulk, tw_tickers))
    if us_tickers:
        fetch_coros.append(asyncio.to_thread(_fetch_us_realtime_bulk, us_tickers))

    if fetch_coros:
        fetch_results = await asyncio.gather(*fetch_coros, return_exceptions=True)
        idx = 0
        if tw_tickers:
            r = fetch_results[idx]; idx += 1
            tw_raw = r if isinstance(r, dict) else {}
            if isinstance(r, Exception):
                logger.warning(f"_fetch_tw_realtime_bulk 失敗: {r}")
        if us_tickers:
            r = fetch_results[idx]; idx += 1
            us_raw = r if isinstance(r, dict) else {}
            if isinstance(r, Exception):
                logger.warning(f"_fetch_us_realtime_bulk 失敗: {r}")

    # ── 組合有效結果 ──
    data_list: list = []
    for e in tw_pool:
        q = tw_raw.get(e["ticker"])
        if q:
            data_list.append({"ticker": e["ticker"], "market": "TW", **q})
    for e in us_pool:
        q = us_raw.get(e["ticker"])
        if q:
            data_list.append({"ticker": e["ticker"], "market": "US", **q})

    if not data_list:
        logger.debug("⚡ 批量快速報價 tick：無有效報價，略過 DB 寫入")
        return

    # ── 批次寫入（含 dirty check），回傳實際寫入列數 ──
    try:
        written = await asyncio.to_thread(save_price_bulk, data_list)
    except Exception as e:
        logger.warning(f"save_price_bulk 失敗: {e}", exc_info=True)
        written = 0

    logger.debug(
        f"⚡ 批量快速報價 tick 完成：抓到 {len(data_list)} 檔，"
        f"實際寫入 {written} 檔（{len(data_list)-written} 檔 dirty check 跳過）"
    )

    # ── 只要有新資料寫入，立即掃描到價提醒 ──
    if written > 0:
        await _check_price_alerts()


# ──────────────────────────────────────────────
#  盤中完整資料 tick（每 30 分鐘）：含補充資料
# ──────────────────────────────────────────────

async def _market_tick():
    """盤中完整資料更新（每 30 分鐘）：
    抓取完整 ETF 資料含 dividend、歷史月線、費用率等補充資料。
    現價更新由 _fast_price_tick（每 2 分鐘）負責，此函式不重複掃描 alert。
    """
    try:
        await _market_tick_inner()
    except Exception as e:
        logger.error(f"_market_tick 未預期例外（排程器仍繼續運行）: {e}", exc_info=True)


async def _market_tick_inner():
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
        # TiDB/MySQL 不支援 NULLS FIRST，使用 ISNULL() DESC 讓 NULL 優先
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
                      OR d.last_date < DATE_SUB(CURDATE(), INTERVAL 1 DAY)
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

    logger.info(f"★ 補漏掃描：找到 {len(rows)} 檔待補抓（超過 1 天未更新）")

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
#  TWSE 歷史價格補齊（每日 03:00 增量補缺月份）
#  US ETF 歷史補齊（每日 04:30 美股收盤後）
# ──────────────────────────────────────────────

def schedule_history_backfill():
    if MAIN_LOOP and MAIN_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(_run_history_backfill(), MAIN_LOOP)


def schedule_us_history_backfill():
    if MAIN_LOOP and MAIN_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(_run_us_history_backfill(), MAIN_LOOP)


def schedule_returns_recalc():
    if MAIN_LOOP and MAIN_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(_run_returns_recalc(), MAIN_LOOP)


async def _run_history_backfill():
    """增量補齊 TW ETF 歷史收盤價（只補尚未存在的月份，冪等安全）。"""
    from services.twse_history import backfill_tw_history
    try:
        result = await asyncio.to_thread(backfill_tw_history)
        logger.info(f"✅ 歷史補齊完成：{result['etfs']} 檔，補 {result['days_inserted']} 日")
    except Exception as e:
        logger.warning(f"歷史補齊失敗: {e}")


async def _run_us_history_backfill():
    """增量補齊 US ETF 歷史收盤價（透過 CF 代理，只補尚未存在的日期，冪等安全）。"""
    from services.us_history import backfill_us_history
    try:
        result = await asyncio.to_thread(backfill_us_history)
        logger.info(f"✅ US 歷史補齊完成：{result['etfs']} 檔，補 {result['days_inserted']} 日")
    except Exception as e:
        logger.warning(f"US 歷史補齊失敗: {e}")


async def _run_returns_recalc():
    """從 DB 歷史收盤價重算年化報酬率（不依賴 Yahoo Finance）。
    在 TWSE 歷史補齊（03:00）之後 30 分鐘執行，確保最新日資料已入庫。
    """
    from services.returns_calc import recalc_all_returns
    try:
        result = await asyncio.to_thread(recalc_all_returns)
        logger.info(f"✅ 年化報酬率重算完成：更新 {result['updated']} 檔，略過 {result['skipped']} 檔")
        # 清除排行榜快取，讓下次請求取得最新報酬率排名
        from cache import cache
        cache.delete_prefix("rank:")
    except Exception as e:
        logger.warning(f"年化報酬率重算失敗: {e}")


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
    from services.alerts import check_price_alerts
    from database import get_db
    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT pa.ticker, d.current_price
                FROM price_alerts pa
                JOIN (
                    SELECT d1.ticker, d1.current_price FROM etf_daily_data d1
                    INNER JOIN (
                        SELECT ticker, MAX(date) as md FROM etf_daily_data
                        WHERE current_price > 0 GROUP BY ticker
                    ) d2 ON d1.ticker=d2.ticker AND d1.date=d2.md
                ) d ON pa.ticker=d.ticker
                WHERE pa.is_active=1 AND pa.is_triggered=0
                GROUP BY pa.ticker
            """)
            rows = cursor.fetchall()
        for row in rows:
            check_price_alerts(row["ticker"], float(row["current_price"]))
    except Exception as e:
        logger.warning(f"_check_price_alerts 失敗: {e}")


# ──────────────────────────────────────────────
#  user_sessions 清理（防止表格無限增長拖慢 JTI 查詢）
# ──────────────────────────────────────────────

def schedule_session_cleanup():
    if MAIN_LOOP and MAIN_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(_run_session_cleanup(), MAIN_LOOP)


async def _run_session_cleanup():
    """刪除過期的 JWT session 紀錄（expires_at < NOW()）。
    token 本身的 exp claim 已過期 → JWT 驗證自然拒絕 → 對應 session 行無需保留。
    每日 02:00 執行，防止 user_sessions 表無限累積拖慢 JTI 索引查詢。
    """
    from database import get_db
    try:
        with get_db() as (conn, cursor):
            cursor.execute("DELETE FROM user_sessions WHERE expires_at < NOW()")
            deleted = cursor.rowcount
            conn.commit()
        if deleted:
            logger.info(f"✅ Session 清理：刪除 {deleted} 筆過期記錄")
        else:
            logger.debug("Session 清理：無過期記錄")
    except Exception as e:
        logger.warning(f"Session 清理失敗: {e}")


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

    # 每 30 秒：批量快速報價更新 + 到價提醒掃描（開盤期間）
    # TW：TWSE MIS 批量 API（1-2 次 HTTP）；US：Yahoo v7/quote?symbols=（1 次 HTTP）
    # dirty check 跳過未變動標的；executemany 1 次 DB 往返；整批 < 3 秒
    sch.add_job(
        lambda: schedule_fast_price_tick(),
        "interval", seconds=30,
        id="fast_price_tick", max_instances=1,
    )

    # 每 30 分鐘：完整資料更新含 dividend/history/費用率（開盤期間）
    sch.add_job(
        lambda: (
            _is_any_market_open() and
            MAIN_LOOP and MAIN_LOOP.is_running() and
            asyncio.run_coroutine_threadsafe(_market_tick(), MAIN_LOOP)   # wrapper 已含 try-except
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

    # 05:15 美股收盤後完整更新收盤資料
    # 美東 16:00 收盤：EDT(UTC-4)=台灣 04:00；EST(UTC-5)=台灣 05:00
    # 取 05:15 確保冬令（EST）也在收盤後執行，夏令延遲 75 分鐘仍可接受
    sch.add_job(lambda: schedule_update(),    CronTrigger(hour=5,  minute=15), max_instances=1)

    # 每日 03:00 增量補齊 TW ETF 歷史收盤價（只補缺月，冪等）
    sch.add_job(lambda: schedule_history_backfill(), CronTrigger(hour=3, minute=0), max_instances=1)

    # 每日 03:30 從 DB 歷史收盤價重算年化報酬率（不依賴 Yahoo，補齊 03:00 補填的資料）
    sch.add_job(lambda: schedule_returns_recalc(),   CronTrigger(hour=3, minute=30), max_instances=1)

    # 每日 05:30 增量補齊 US ETF 歷史收盤價（05:15 美股收盤更新完成後啟動，透過 CF 代理）
    sch.add_job(lambda: schedule_us_history_backfill(), CronTrigger(hour=5, minute=30), max_instances=1)

    # 每日 05:50 US 歷史補齊後重算年化報酬率（確保 US ETF returns 在台灣早上有最新值）
    sch.add_job(lambda: schedule_returns_recalc(), CronTrigger(hour=5, minute=50), max_instances=1)

    # 14:40 台股收盤後再次重算報酬率（取得當日最新收盤後重算）
    sch.add_job(lambda: schedule_returns_recalc(),   CronTrigger(hour=14, minute=40), max_instances=1)

    # 每日 02:00 清理過期 user_sessions（防止表格無限增長拖慢 JTI 查詢）
    sch.add_job(lambda: schedule_session_cleanup(), CronTrigger(hour=2, minute=0), max_instances=1)

    # 快取維護（每 30 分鐘）
    sch.add_job(_evict_cache, "interval", minutes=30, max_instances=1)

    sch.start()
    logger.info(
        "✅ 排程器已啟動\n"
        "   【盤中快速】每 30 秒批量更新現價 + 掃描到價提醒（開盤期間）\n"
        "   【盤中完整】每 30 分鐘更新含 dividend/history 補充資料\n"
        "   【台股】09:00–13:30 Asia/Taipei\n"
        "   【美股】09:30–16:00 America/New_York（DST-aware）\n"
        "   03:00 TWSE 歷史補齊 | 03:30 報酬率重算 | 07:00 補漏掃描 | 08:00 TWSE 同步\n"
        "   14:35 台股收盤完整更新 | 14:40 報酬率重算 | 05:15 美股收盤完整更新 | 05:30 US 歷史補齊 | 05:50 報酬率重算 | 每30分清快取"
    )
    return sch
