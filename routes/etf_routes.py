"""
routes/etf_routes.py — ETF 清單、詳情、搜尋、排行榜、歷史
"""
import asyncio, logging, time
from datetime import datetime, timedelta, timezone, date
from typing import Optional
from fastapi import APIRouter, Request, Query, BackgroundTasks
from fastapi.templating import Jinja2Templates

import yfinance as yf
import pandas as pd

from models import EtfAddIn
from database import get_db
from utils import safe_json, safe_float
from cache import cache, CACHE_TTL_RANK, CACHE_TTL_DETAIL
import requests as _req
import certifi as _certifi

from etf_data import fetch_one_etf, save_etf_data, _yahoo_ticker, _new_session, _cf_yahoo_get
from services.alerts import check_dip_alert
from services.exchange_rate import get_usd_twd

logger = logging.getLogger(__name__)
router = APIRouter()
templates: Jinja2Templates | None = None

# 動態爬蟲 rate limiter：同一 IP 每 60 秒最多觸發 3 次，防止 Yahoo Finance 封鎖
# 使用 MemCache 而非全域 dict，確保 TTL 自動清理，防止記憶體無限增長
_RATE_WINDOW  = 60
_RATE_MAX     = 3

def _check_demand_rate(client_ip: str) -> bool:
    """若超過速率限制回傳 True。使用 cache 儲存時間戳，TTL 到期自動清理。"""
    if not client_ip or client_ip == "unknown":
        return False
    key = f"rate:demand:{client_ip}"
    timestamps: list = cache.get(key) or []
    now = time.time()
    timestamps = [t for t in timestamps if now - t < _RATE_WINDOW]
    if len(timestamps) >= _RATE_MAX:
        cache.set(key, timestamps, _RATE_WINDOW)
        return True
    timestamps.append(now)
    cache.set(key, timestamps, _RATE_WINDOW)
    return False

LATEST_DAILY_JOIN = """
LEFT JOIN (
    SELECT d1.* FROM etf_daily_data d1
    INNER JOIN (
        SELECT ticker, MAX(date) AS max_date
        FROM etf_daily_data
        WHERE current_price > 0
        GROUP BY ticker
    ) d2 ON d1.ticker = d2.ticker AND d1.date = d2.max_date
) d ON m.ticker = d.ticker
"""

_ETF_DETAIL_SELECT = """
    SELECT m.ticker, m.name, m.market,
        m.issuer, m.listing_date,
        COALESCE(d.current_price,0) as current_price,
        COALESCE(d.price_change,0) as price_change,
        COALESCE(d.price_change_percent,0) as price_change_percent,
        COALESCE(d.volume,0) as volume,
        COALESCE(d.nav,0) as nav,
        COALESCE(d.discount_premium,0) as discount_premium,
        COALESCE(d.dividend_yield,0) as dividend_yield,
        COALESCE(d.payout_freq,'不配息') as payout_freq,
        d.annual_return_1y,   -- NULL = 資料不足（前端顯示「—」）
        d.annual_return_3y,
        d.annual_return_5y,
        COALESCE(d.pe_ratio,0) as pe_ratio,
        COALESCE(d.expense_ratio,0) as expense_ratio,
        COALESCE(d.day_high,0) as day_high,
        COALESCE(d.day_low,0) as day_low,
        COALESCE(d.fifty_two_week_high,0) as fifty_two_week_high,
        COALESCE(d.fifty_two_week_low,0) as fifty_two_week_low,
        d.date as data_date
    FROM etf_master m
    {join}
    WHERE m.ticker=%s
"""

def _fetch_etf_detail_row(cursor, ticker: str) -> Optional[dict]:
    """查詢單一 ETF 詳情，避免在同一請求內重複撰寫相同的 SQL。"""
    cursor.execute(_ETF_DETAIL_SELECT.format(join=LATEST_DAILY_JOIN), (ticker,))
    return cursor.fetchone()


# ── 頁面 ──

@router.get("/")
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@router.get("/etf-list")
async def etf_list_page(request: Request):
    return templates.TemplateResponse("etf_list.html", {"request": request})

@router.get("/etf-detail/{ticker}")
async def etf_detail_page(request: Request, ticker: str):
    return templates.TemplateResponse("etf-detail.html", {"request": request, "ticker": ticker.upper()})

@router.get("/watchlist")
async def watchlist_page(request: Request):
    return templates.TemplateResponse("watchlist.html", {"request": request})

@router.get("/portfolio")
async def portfolio_page(request: Request):
    return templates.TemplateResponse("portfolio.html", {"request": request})

@router.get("/profile")
async def profile_page(request: Request):
    return templates.TemplateResponse("profile.html", {"request": request})

@router.get("/notifications")
async def notifications_page(request: Request):
    return templates.TemplateResponse("notifications.html", {"request": request})


# ── 排行榜 ──

# 排行榜共用 SELECT（annual_return_1y 不用 COALESCE，保留 NULL 讓前端顯示「—」）
_RANK_SELECT = """
    SELECT m.ticker, m.name, m.market,
        COALESCE(d.current_price,0)          AS current_price,
        COALESCE(d.price_change,0)           AS price_change,
        COALESCE(d.price_change_percent,0)   AS price_change_percent,
        COALESCE(d.volume,0)                 AS volume,
        COALESCE(d.dividend_yield,0)         AS dividend_yield,
        COALESCE(d.payout_freq,'不配息')      AS payout_freq,
        d.annual_return_1y,
        COALESCE(d.expense_ratio,0)          AS expense_ratio
    FROM etf_master m
    {join}
    WHERE d.current_price IS NOT NULL AND d.current_price > 0
      AND COALESCE(m.is_delisted, 0) = 0
      AND m.is_hot = 1
      AND m.market = %s
    ORDER BY {order}
    LIMIT 10
"""

_RANK_ORDER = {
    "volume": "d.volume DESC",
    # NULL 排最後（資料不足）→ 有值的從高到低排
    "return": "d.annual_return_1y IS NULL ASC, COALESCE(d.annual_return_1y,0) DESC",
    "yield":  "d.dividend_yield   IS NULL ASC, COALESCE(d.dividend_yield,0)   DESC",
}


@router.get("/api/etf-rankings/{rank_type}")
async def get_etf_rankings(rank_type: str, market: str = ""):
    """舊端點：向下相容（前端新版改用 /api/etf/rankings/combined）"""
    market = market.upper().strip() if market.upper().strip() in ("TW", "US") else "TW"
    cache_key = f"rank:{rank_type}:{market}"
    cached = cache.get(cache_key)
    if cached:
        return safe_json({"status": "success", **cached})

    try:
        order = _RANK_ORDER.get(rank_type, _RANK_ORDER["volume"])
        with get_db() as (conn, cursor):
            cursor.execute(
                _RANK_SELECT.format(join=LATEST_DAILY_JOIN, order=order),
                (market,)
            )
            rows = cursor.fetchall()

        payload = {"data": rows, "updated_at": datetime.now().strftime('%H:%M')}
        cache.set(cache_key, payload, CACHE_TTL_RANK)
        return safe_json({"status": "success", **payload})
    except Exception as e:
        logger.error(f"etf rankings error ({rank_type}/{market}): {e}", exc_info=True)
        return safe_json({"status": "success", "data": [], "updated_at": datetime.now().strftime('%H:%M')})


@router.get("/api/etf/rankings/combined")
async def get_combined_rankings(market: str = "TW"):
    """一次回傳全部 3 種排行（成交量/年化報酬/殖利率），前端切 Tab 不需重打 API。"""
    market = market.upper() if market.upper() in ("TW", "US") else "TW"
    cache_key = f"rank:combined:{market}"
    cached = cache.get(cache_key)
    if cached:
        return safe_json({"status": "success", **cached})

    try:
        result: dict = {}
        with get_db() as (conn, cursor):
            for rank_type, order in _RANK_ORDER.items():
                cursor.execute(
                    _RANK_SELECT.format(join=LATEST_DAILY_JOIN, order=order),
                    (market,)
                )
                result[rank_type] = cursor.fetchall()

        result["updated_at"] = datetime.now().strftime('%H:%M')
        cache.set(cache_key, result, CACHE_TTL_RANK)
        return safe_json({"status": "success", **result})
    except Exception as e:
        logger.error(f"combined rankings error (market={market}): {e}", exc_info=True)
        # 回傳空成功，讓前端顯示「暫無資料」而非無限轉圈
        return safe_json({
            "status": "success",
            "volume": [], "return": [], "yield": [],
            "updated_at": datetime.now().strftime('%H:%M'),
        })


@router.get("/api/etf/rankings/all")
async def get_all_rankings():
    """一次回傳 TW + US 全部排行，前端只需 1 個 API 請求（而非 2）。
    快取 TTL 與 combined 相同（CACHE_TTL_RANK）。
    """
    cached = cache.get("rank:all")
    if cached:
        return safe_json({"status": "success", **cached})

    try:
        result: dict = {"TW": {}, "US": {}}
        with get_db() as (conn, cursor):
            for market in ("TW", "US"):
                for rank_type, order in _RANK_ORDER.items():
                    cursor.execute(
                        _RANK_SELECT.format(join=LATEST_DAILY_JOIN, order=order),
                        (market,)
                    )
                    result[market][rank_type] = cursor.fetchall()

        result["updated_at"] = datetime.now().strftime('%H:%M')
        cache.set("rank:all", result, CACHE_TTL_RANK)
        return safe_json({"status": "success", **result})
    except Exception as e:
        logger.error(f"all rankings error: {e}", exc_info=True)
        return safe_json({
            "status": "success",
            "TW": {"volume": [], "return": [], "yield": []},
            "US": {"volume": [], "return": [], "yield": []},
            "updated_at": datetime.now().strftime('%H:%M'),
        })


@router.get("/api/etf/index")
async def get_etf_index():
    """輕量 ETF 清單（只含 ticker/name/market），供前端本地搜尋/自動補全用。
    無需每次打字都 DB 查詢，Client 端過濾速度提升 20x 以上。
    """
    cached = cache.get("etf:index")
    if cached:
        return safe_json({"status": "success", **cached})

    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT ticker, name, market, COALESCE(is_hot,0) AS is_hot
                FROM etf_master
                WHERE COALESCE(is_delisted, 0) = 0
                ORDER BY is_hot DESC, ticker
            """)
            rows = cursor.fetchall()
            cursor.execute("""
                SELECT COUNT(DISTINCT d.ticker) AS cnt
                FROM etf_dividends d
                JOIN etf_master m ON m.ticker = d.ticker
                WHERE m.is_hot = 1 AND m.is_delisted = 0
            """)
            div_row = cursor.fetchone()
            divs_count = int((div_row or {}).get("cnt") or 0)

        payload = {"data": rows, "divs_count": divs_count}
        cache.set("etf:index", payload, 1800)  # 30 分鐘快取
        return safe_json({"status": "success", **payload})
    except Exception as e:
        logger.error(f"etf index error: {e}", exc_info=True)
        return safe_json({"status": "success", "data": []})


# ── 搜尋 ──

@router.get("/api/etf/search")
async def search_etf(request: Request, q: str = Query(..., min_length=1)):
    q_up = q.upper().strip()
    cache_key = f"search:{q_up}"
    cached = cache.get(cache_key)
    if cached:
        return safe_json({"status": "success", "data": cached})

    try:
        with get_db() as (conn, cursor):
            cursor.execute(f"""
                SELECT m.ticker, m.name, m.market,
                    COALESCE(d.current_price,0) as current_price,
                    COALESCE(d.price_change_percent,0) as price_change_percent,
                    COALESCE(d.dividend_yield,0) as dividend_yield,
                    COALESCE(d.payout_freq,'不配息') as payout_freq,
                    d.annual_return_1y
                FROM etf_master m
                {LATEST_DAILY_JOIN}
                WHERE (m.ticker LIKE %s OR m.name LIKE %s)
                  AND COALESCE(m.is_delisted, 0) = 0
                ORDER BY
                    CASE WHEN m.ticker = %s     THEN 0
                         WHEN m.ticker LIKE %s  THEN 1
                         ELSE                        2 END,
                    m.ticker
                LIMIT 30
            """, (f"%{q_up}%", f"%{q}%", q_up, f"{q_up}%"))
            rows = cursor.fetchall()
    except Exception as e:
        logger.warning(f"search_etf DB error: {e}")
        return safe_json({"status": "success", "data": []})

    # ── 隨需探索：資料庫找不到且輸入看起來像代碼 ──
    # 改為 fire-and-forget：背景觸發後立即回傳「尚未找到」，下次搜尋時會有結果
    # （不再 await 30 秒，避免搜尋框卡死）
    if not rows and _looks_like_ticker(q_up):
        fwd = request.headers.get("x-forwarded-for", "")
        client_ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
        if not _check_demand_rate(client_ip):
            # 觸發背景探索，不等待結果
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_on_demand_fetch(q_up, client_ip))
            except Exception:
                pass

    cache.set(cache_key, rows, 60)
    return safe_json({"status": "success", "data": rows})


def _looks_like_ticker(s: str) -> bool:
    """判斷字串是否可能是代碼（非中文搜尋詞）"""
    if len(s) > 10 or len(s) < 2:
        return False
    import re
    return bool(re.match(r'^[A-Z0-9.]+$', s))


async def _on_demand_fetch(ticker: str, client_ip: str = "") -> Optional[dict]:
    """即時向 Yahoo Finance 探索未知代碼，確認後寫入 etf_master。
    rate limit：同一 IP 60 秒內最多觸發 3 次，防止 Yahoo Finance 封鎖主機 IP。
    """
    if client_ip and _check_demand_rate(client_ip):
        logger.warning(f"動態爬蟲 rate limit 已觸發 for {client_ip}")
        return None

    market = "TW" if ticker[:4].isdigit() else "US"

    def _do():
        try:
            data = fetch_one_etf(ticker, market)
            if not data or not data.get("current_price"):
                return None
            data["ticker"] = ticker
            # 寫入 master（標記自動發現）
            with get_db() as (conn, cursor):
                cursor.execute(
                    "INSERT IGNORE INTO etf_master (ticker, name, market, auto_discovered) "
                    "VALUES (%s, %s, %s, 1)",
                    (ticker, data.get("name", ticker), market),
                )
                conn.commit()
            save_etf_data(data)
            logger.info(f"🔍 隨需探索成功：{ticker} ({market})")
            return {
                "ticker": ticker,
                "name": data.get("name", ticker),
                "market": market,
                "current_price": data.get("current_price", 0),
                "price_change_percent": data.get("price_change_percent", 0),
                "dividend_yield": data.get("dividend_yield", 0),
                "payout_freq": data.get("payout_freq", ""),
                "annual_return_1y": data.get("annual_return_1y"),  # None 保留，前端 retFmt 顯示「—」
            }
        except Exception as e:
            logger.warning(f"_on_demand_fetch {ticker}: {e}")
            return None

    return await asyncio.to_thread(_do)


@router.get("/api/etf/search/dynamic")
async def dynamic_search(request: Request, q: str = Query(..., min_length=1)):
    return await search_etf(request, q)


# ── 強制重算單檔（登入即可用，用於驗證修正 / 手動刷新）──

@router.post("/api/etf/force-refresh/{ticker}")
async def force_refresh_etf(ticker: str, request: Request, background_tasks: BackgroundTasks):
    """強制重新計算單一 ETF 年化報酬 + 52 週高低，立即返回、背景執行（不再超時）。
    TW ETF：並行抓 TWSE 歷史收盤 → 計算報酬率 → 更新 DB
    US ETF：Yahoo Finance → 完整更新
    """
    from auth import get_current_user
    from fastapi import HTTPException
    try:
        # credentials=None → 略過 Bearer header，直接走 Cookie 驗證
        # 若傳入 Depends(_bearer) 物件（預設值）會因無 .scheme 屬性而 AttributeError
        get_current_user(request, credentials=None)
    except HTTPException:
        return safe_json({"status": "error", "message": "請先登入"}, 401)

    ticker = ticker.upper().strip()
    rate_key = f"force_refresh:{ticker}"
    if cache.get(rate_key):
        return safe_json({"status": "error", "message": f"{ticker} 60 秒內已刷新過，請稍候"}, 429)

    market = "TW" if ticker[:4].isdigit() else "US"
    current_price = 0.0
    try:
        # etf_master 只有 market 欄，current_price 在 etf_daily_data
        with get_db() as (conn, cursor):
            cursor.execute("SELECT market FROM etf_master WHERE ticker=%s LIMIT 1", (ticker,))
            row = cursor.fetchone()
            if row:
                market = row["market"]
            # 從 etf_daily_data 取最新現價（etf_master 沒有此欄）
            cursor.execute(
                "SELECT current_price FROM etf_daily_data "
                "WHERE ticker=%s AND current_price>0 ORDER BY date DESC LIMIT 1",
                (ticker,),
            )
            prow = cursor.fetchone()
            if prow:
                current_price = float(prow["current_price"] or 0)
    except Exception:
        pass

    cache.set(rate_key, 1, 60)   # 立即上鎖，防止重複觸發

    if market == "TW":
        def _bg_tw():
            """背景任務：並行抓 TWSE 歷史收盤 → 計算年化報酬 + 52W H/L → 更新 etf_daily_data"""
            import concurrent.futures
            from dateutil.relativedelta import relativedelta as _rd
            from etf_data import _fetch_tw_realtime_perfect, _fetch_52week_hl_db

            try:
                # 1. 取最新現價（TWSE mis，~1s）
                realtime = _fetch_tw_realtime_perfect(ticker)
                price = realtime.get("current_price", 0) if realtime else current_price
                if not price or price <= 0:
                    price = current_price
                if not price or price <= 0:
                    logger.warning(f"force_refresh {ticker}: 無法取得現價，放棄")
                    return

                # 2. 並行抓 1Y / 3Y / 5Y 歷史收盤（各抓目標月份最後一筆）
                def _close_n_months_ago(n: int) -> Optional[float]:
                    target = date.today() - _rd(months=n)
                    rows = _fetch_twse_month(ticker, target.year, target.month)
                    return rows[-1]["close"] if rows else None

                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                    f1y = pool.submit(_close_n_months_ago, 12)
                    f3y = pool.submit(_close_n_months_ago, 36)
                    f5y = pool.submit(_close_n_months_ago, 60)
                    p1y = f1y.result(timeout=25)
                    p3y = f3y.result(timeout=25)
                    p5y = f5y.result(timeout=25)

                # 3. 計算年化報酬
                ann_1y = round((price / p1y - 1) * 100, 2)              if p1y and p1y > 0 else None
                ann_3y = round(((price / p3y) ** (1 / 3) - 1) * 100, 2) if p3y and p3y > 0 else None
                ann_5y = round(((price / p5y) ** (1 / 5) - 1) * 100, 2) if p5y and p5y > 0 else None

                # 4. 52 週高低（從 DB 歷史 current_price 計算）
                wk52_h, wk52_l = _fetch_52week_hl_db(ticker, price)

                # 5. 寫入 etf_daily_data（正確的表）
                today_str = date.today().isoformat()
                with get_db() as (conn, cursor):
                    # 先確保今天有一筆基礎記錄
                    cursor.execute(
                        "INSERT INTO etf_daily_data (ticker, date, current_price, "
                        "price_change, price_change_percent) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE "
                        "current_price=IF(VALUES(current_price)>0,VALUES(current_price),current_price)",
                        (ticker, today_str, price,
                         realtime.get("price_change", 0) if realtime else 0,
                         realtime.get("price_change_percent", 0) if realtime else 0),
                    )
                    # 更新年化報酬與 52W H/L
                    set_parts, vals = [], []
                    if ann_1y is not None:
                        set_parts.append("annual_return_1y=%s"); vals.append(ann_1y)
                    if ann_3y is not None:
                        set_parts.append("annual_return_3y=%s"); vals.append(ann_3y)
                    if ann_5y is not None:
                        set_parts.append("annual_return_5y=%s"); vals.append(ann_5y)
                    if wk52_h > 0:
                        set_parts.append("fifty_two_week_high=%s"); vals.append(wk52_h)
                    if wk52_l > 0:
                        set_parts.append("fifty_two_week_low=%s");  vals.append(wk52_l)
                    if set_parts:
                        cursor.execute(
                            f"UPDATE etf_daily_data SET {', '.join(set_parts)} "
                            f"WHERE ticker=%s AND date=%s",
                            vals + [ticker, today_str],
                        )
                    conn.commit()

                cache.delete(f"detail:{ticker}")
                cache.delete_prefix("search:")
                cache.delete_prefix("rank:")
                logger.info(f"✅ force_refresh {ticker} 完成: 1y={ann_1y}% 3y={ann_3y}% 52W={wk52_h}/{wk52_l}")

            except Exception as e:
                logger.error(f"force_refresh {ticker} 背景任務失敗: {e}")

        background_tasks.add_task(_bg_tw)

    else:
        def _bg_us():
            data = fetch_one_etf(ticker, market)
            if data:
                save_etf_data(data)
                cache.delete(f"detail:{ticker}")
                cache.delete_prefix("search:")

        background_tasks.add_task(_bg_us)

    return safe_json({
        "status":  "processing",
        "ticker":  ticker,
        "message": f"{ticker} 正在背景重新計算，約 10 秒後重新整理頁面即可看到最新數字",
    })


# ── ETF 詳情 ──

@router.get("/api/etf/detail/{ticker}")
async def get_etf_detail(ticker: str):
    ticker = ticker.upper()
    cache_key = f"detail:{ticker}"
    cached = cache.get(cache_key)
    if cached:
        return safe_json({"status": "success", "data": cached})

    # ── 1. 先查 DB（不在此處呼叫 FX，避免 TW ETF 白跑一次同步 HTTP）──
    try:
        with get_db() as (conn, cursor):
            row = _fetch_etf_detail_row(cursor, ticker)
    except Exception as e:
        logger.error(f"etf detail DB error ({ticker}): {e}", exc_info=True)
        return safe_json({"status": "error", "message": "資料庫暫時無法連線，請稍後再試"}, 503)

    if not row:
        # 資料庫找不到 → 嘗試即時爬取並寫入
        logger.info(f"detail cache miss: {ticker}，觸發即時爬取")
        discovered = await _on_demand_fetch(ticker)
        if not discovered:
            return safe_json({"status": "error", "message": f"找不到 ETF {ticker}，請確認代碼是否正確"}, 404)
        # 爬取後重查 DB（共用相同 SQL 函數）
        with get_db() as (conn, cursor):
            row = _fetch_etf_detail_row(cursor, ticker)
        if not row:
            return safe_json({"status": "error", "message": f"找不到 ETF {ticker}"}, 404)

    # ── 2. 僅 US ETF 才需要 FX，且以 asyncio.to_thread 執行避免阻塞 event loop ──
    if row.get("market") == "US":
        try:
            usd_twd = await asyncio.to_thread(get_usd_twd)
        except Exception:
            usd_twd = 32.0
        row["price_twd"] = round(float(row.get("current_price", 0)) * usd_twd, 2)
        row["usd_twd_rate"] = usd_twd

    # 資料明顯過期（> 1 天）或關鍵欄位為 0，背景靜默更新一次
    _maybe_background_refresh(ticker, row)

    cache.set(cache_key, row, CACHE_TTL_DETAIL)
    return safe_json({"status": "success", "data": row})


_REFRESH_COOLDOWN = 300          # 同一 ticker 5 分鐘內最多觸發一次

def _maybe_background_refresh(ticker: str, row: dict):
    """若資料超過 1 天或關鍵欄位缺失，在背景靜默重抓一次。

    使用 cache 作為跨執行緒共享鎖（取代 process-level set/dict）。
    鎖在 create_task 前設定，防止同一 ticker 在事件循環中建立多個重複任務。
    """
    import asyncio
    from datetime import date as _date

    lock_key = f"bg_refresh:{ticker}"
    if cache.get(lock_key):
        return  # 冷卻中或正在更新

    data_date = row.get("data_date")
    try:
        if isinstance(data_date, str):
            data_date = datetime.strptime(data_date, "%Y-%m-%d").date()
        is_stale = (not data_date) or ((_date.today() - data_date).days >= 1)
    except Exception:
        is_stale = True
    missing_returns = (row.get("annual_return_1y") is None and
                       row.get("annual_return_3y") is None)
    if not (is_stale or missing_returns):
        return

    # 設定鎖後才建立任務，防止多個請求同時進來時重複排程
    cache.set(lock_key, 1, _REFRESH_COOLDOWN)

    async def _do():
        try:
            market = row.get("market") or ("TW" if ticker[:4].isdigit() else "US")
            data = await asyncio.to_thread(fetch_one_etf, ticker, market)
            if data and data.get("current_price"):
                data["ticker"] = ticker
                data["market"] = market
                save_etf_data(data)
                cache.delete(f"detail:{ticker}")
                logger.info(f"🔄 背景更新 {ticker} 完成")
        except Exception as e:
            logger.debug(f"背景更新 {ticker}: {e}")
            cache.delete(lock_key)  # 失敗時釋放鎖，允許更快重試

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do())
    except RuntimeError:
        cache.delete(lock_key)  # 無 running loop → 立即釋放
    except Exception:
        cache.delete(lock_key)


# ── 歷史走勢 ──

# DB 需達到的最低有效點數，才足以畫出有意義的走勢圖（避免 2-3 個連續相近點畫成直線）
_MIN_CHART_ROWS = {
    # 門檻降低：有資料就顯示，不讓「資料稍少」導致白屏
    "1M": 3, "3M": 5, "6M": 10, "YTD": 5,
    "1Y": 20, "3Y": 30, "5Y": 30, "ALL": 10, "MAX": 10,
}

_TWSE_HIST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; ETF-System/2.0)",
}


def _fetch_db_price_history(ticker: str, period: str) -> Optional[dict]:
    """從 etf_daily_data 取歷史收盤價。
    需達到 _MIN_CHART_ROWS 門檻，否則返回 None 讓 TWSE 抓取真實歷史。

    1D / 5D 不在此 fallback 範圍：DB 無分鐘級資料，直接回 None，
    由上層用 Yahoo Finance 即時來源。
    """
    p_up = period.upper()
    # 1D/5D 需要分鐘級資料，DB 只有日線 → 明確不支援，回 None
    if p_up in ("1D", "5D"):
        return None

    today = date.today()
    PERIOD_DAYS = {
        "1M": 35, "3M": 95, "6M": 185,
        "YTD": (today - date(today.year, 1, 1)).days + 1,
        "1Y": 370, "3Y": 1100, "5Y": 1830, "ALL": 9999, "MAX": 9999,
    }
    days = PERIOD_DAYS.get(p_up, 370)
    since = today - timedelta(days=days)
    min_rows = _MIN_CHART_ROWS.get(p_up, 60)
    try:
        with get_db() as (conn, cursor):
            cursor.execute(
                "SELECT date, current_price FROM etf_daily_data "
                "WHERE ticker=%s AND date >= %s AND current_price > 0 "
                "ORDER BY date ASC",
                (ticker, since.strftime("%Y-%m-%d")),
            )
            rows = cursor.fetchall()
        if len(rows) < min_rows:
            logger.debug(f"DB price history {ticker} period={p_up}: only {len(rows)} rows (need {min_rows})")
            return None
        labels = [str(r["date"])[:10] for r in rows]
        prices = [round(float(r["current_price"]), 2) for r in rows]
        # is_partial=True 表示資料未涵蓋完整請求期間（DB 尚在補齊中），前端可顯示提示
        expected_rows = _MIN_CHART_ROWS.get(p_up, 60) * 3
        is_partial = len(rows) < expected_rows
        return {"labels": labels, "prices": prices, "is_intraday": False, "is_partial": is_partial}
    except Exception as e:
        logger.debug(f"DB price history {ticker}: {e}")
    return None


def _save_history_to_db(ticker: str, days: list[dict]):
    """把 TWSE 抓回的歷史日收盤價寫入 DB，只補空缺不覆蓋現有資料。"""
    if not days:
        return
    try:
        with get_db() as (conn, cursor):
            for d in days:
                cursor.execute(
                    "INSERT INTO etf_daily_data (ticker, date, current_price) "
                    "VALUES (%s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE "
                    "current_price = IF(current_price = 0, VALUES(current_price), current_price)",
                    (ticker, d["date"], d["close"]),
                )
            conn.commit()
    except Exception as e:
        logger.debug(f"save history to DB {ticker}: {e}")


def _fetch_twse_month(ticker: str, year: int, month: int) -> list[dict]:
    """從 TWSE 抓單月日收盤資料，同時嘗試 TSE 與 TPEX。"""
    date_str = f"{year}{month:02d}01"
    # TSE 上市
    for url in [
        f"https://www.twse.com.tw/rwd/zh/stock/STOCK_DAY?stockNo={ticker}&date={date_str}&response=json",
        f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php"
        f"?l=zh-tw&d={year - 1911}/{month:02d}&stkno={ticker}&output=json",
    ]:
        try:
            r = _req.get(url, headers=_TWSE_HIST_HEADERS, timeout=5, verify=_certifi.where())
            if r.status_code != 200:
                continue
            body = r.json()
            # TWSE 格式
            if body.get("stat") == "OK" and body.get("data"):
                result = []
                for row in body["data"]:
                    try:
                        parts = row[0].strip().split("/")
                        iso_date = f"{int(parts[0]) + 1911}-{parts[1]}-{parts[2]}"
                        raw = row[6].strip().replace(",", "")
                        close = float(''.join(c for c in raw if c.isdigit() or c == '.'))
                        if close > 0:
                            result.append({"date": iso_date, "close": close})
                    except Exception:
                        continue
                if result:
                    return result
            # TPEX 格式（欄位不同）
            tpex_data = body.get("aaData") or body.get("data") or []
            if tpex_data:
                result = []
                for row in tpex_data:
                    try:
                        # TPEX aaData: [日期, 成交股數, 成交金額, 開盤, 最高, 最低, 收盤, 漲跌, 成交筆數]
                        # 收盤價在 index 6（不是 2，2 是成交金額）
                        parts = str(row[0]).strip().split("/")
                        iso_date = f"{int(parts[0]) + 1911}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
                        raw = str(row[6]).strip().replace(",", "")
                        close = float(''.join(c for c in raw if c.isdigit() or c == '.'))
                        if close > 0:
                            result.append({"date": iso_date, "close": close})
                    except Exception:
                        continue
                if result:
                    return result
        except Exception as e:
            logger.debug(f"TWSE/TPEX month {ticker} {year}/{month}: {e}")
    return []


def _fetch_twse_price_history(ticker: str, period: str) -> Optional[dict]:
    """從 TWSE / TPEX 官方 API 抓歷史收盤價（不依賴 Yahoo Finance）。
    以 4 執行緒並行抓取，大幅縮短等待時間；成功後寫入 DB 供下次直接回傳。
    """
    import concurrent.futures
    from dateutil.relativedelta import relativedelta as _rd

    cache_key = f"twse_hist:{ticker}:{period}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    today = date.today()
    PERIOD_MONTHS = {
        "1M": 2, "3M": 4, "6M": 7, "YTD": today.month + 1,
        "1Y": 13, "3Y": 37, "5Y": 61, "ALL": 61, "MAX": 61,
    }
    months_needed = PERIOD_MONTHS.get(period.upper(), 13)

    # 建立需要抓取的月份清單
    targets = [(today - _rd(months=i)) for i in range(months_needed - 1, -1, -1)]

    # 並行抓取（最多 4 個執行緒，避免 TWSE rate limit）
    all_days: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_twse_month, ticker, t.year, t.month): t for t in targets}
        for fut in concurrent.futures.as_completed(futures):
            try:
                all_days.extend(fut.result())
            except Exception:
                pass

    if len(all_days) < 5:
        return None

    # 排序去重
    seen: set = set()
    deduped = []
    for d in sorted(all_days, key=lambda x: x["date"]):
        if d["date"] not in seen:
            seen.add(d["date"])
            deduped.append(d)

    result = {
        "labels": [d["date"] for d in deduped],
        "prices": [round(d["close"], 2) for d in deduped],
        "is_intraday": False,
    }
    cache.set(cache_key, result, ttl=21600)  # 6h
    _save_history_to_db(ticker, deduped)     # 回填 DB，下次 DB 直接回傳
    return result


# price-history 各期間快取 TTL（秒）
# 1D/5D：盤中每 2 分鐘更新；非即時歷史：1M=1h、長期=24h
_HIST_CACHE_TTL = {
    "1D": 120, "5D": 300,
    "1M": 3600, "3M": 3600,
    "6M": 7200, "YTD": 3600,
    "1Y": 86400, "3Y": 86400,
    "5Y": 86400, "ALL": 86400, "MAX": 86400,
}


@router.get("/api/etf/price-history/{ticker}")
async def get_price_history(ticker: str, period: str = "1y"):
    ticker = ticker.upper()
    # 對齊 Yahoo Finance 標準期間（1D 5D 1M 6M YTD 1Y 5Y All）
    RANGE_MAP = {
        "1D":  "1d",   "5D":  "5d",
        "1M":  "1mo",  "3M":  "3mo",   # 3M 保留向下相容
        "6M":  "6mo",  "YTD": "ytd",
        "1Y":  "1y",   "3Y":  "3y",    # 3Y 保留向下相容
        "5Y":  "5y",   "ALL": "max",
        "MAX": "max",
    }
    INTERVAL_MAP = {
        "1D":  "5m",    "5D":  "15m",
        "1M":  "1d",    "3M":  "1d",
        "6M":  "1d",    "YTD": "1d",
        "1Y":  "1d",    "3Y":  "1wk",
        "5Y":  "1wk",   "ALL": "1mo",
        "MAX": "1mo",
    }
    p = period.upper()
    yf_range    = RANGE_MAP.get(p, "1y")
    yf_interval = INTERVAL_MAP.get(p, "1d")
    is_intraday = p in ("1D", "5D")

    # ── 回應快取：第二次起毫秒內回傳 ──
    _hist_cache_key = f"hist:{ticker}:{p}"
    _hist_cached = cache.get(_hist_cache_key)
    if _hist_cached:
        return _hist_cached

    with get_db() as (conn, cursor):
        cursor.execute("SELECT market FROM etf_master WHERE ticker=%s", (ticker,))
        row = cursor.fetchone()
        market = (row or {}).get("market") or ("TW" if ticker[:4].isdigit() else "US")

    yt = _yahoo_ticker(ticker, market)
    # 時區偏移：台股 UTC+8；美股動態判斷 EDT(UTC-4) / EST(UTC-5)
    # DST：3月第二個週日 ~ 11月第一個週日（美東）
    if market == "TW":
        tz_offset_h = 8
    else:
        _now = datetime.now(tz=timezone.utc)
        _y = _now.year
        # 3月第二個週日
        _mar2 = datetime(_y, 3, 8, 7, tzinfo=timezone.utc)
        _mar2 += timedelta(days=(6 - _mar2.weekday()) % 7)
        # 11月第一個週日
        _nov1 = datetime(_y, 11, 1, 6, tzinfo=timezone.utc)
        _nov1 += timedelta(days=(6 - _nov1.weekday()) % 7)
        tz_offset_h = -4 if _mar2 <= _now < _nov1 else -5  # EDT or EST

    # timeout 策略：1D/5D (intraday) 用 5s 快速失敗；非即時歷史資料用 12s（US ETF 1Y 回傳 ~252 筆）
    yahoo_timeout = 5 if is_intraday else 12

    def _fetch():
        symbols = [yt]
        if market == "TW" and yt.endswith(".TW"):
            symbols.append(f"{ticker}.TWO")

        for symbol in symbols:
            try:
                url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
                       f"?range={yf_range}&interval={yf_interval}")
                # CF Proxy 優先（繞過 Railway IP 封鎖），fallback 直連 Yahoo
                r = (_cf_yahoo_get(url, timeout=yahoo_timeout)
                     or _new_session(f"https://finance.yahoo.com/quote/{symbol}").get(url, timeout=yahoo_timeout))
                if r.status_code != 200:
                    continue
                result = r.json().get("chart", {}).get("result")
                if not result:
                    continue
                ts     = result[0].get("timestamp", [])
                closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                pairs  = [(t, c) for t, c in zip(ts, closes) if c is not None]
                if len(pairs) < 2:
                    continue

                if is_intraday:
                    labels = []
                    for t, _ in pairs:
                        dt_local = (datetime.fromtimestamp(t, tz=timezone.utc).replace(tzinfo=None)
                                    + timedelta(hours=tz_offset_h))
                        labels.append(dt_local.strftime("%H:%M") if p == "1D"
                                      else dt_local.strftime("%m/%d %H:%M"))
                else:
                    labels = [datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
                              for t, _ in pairs]

                prices = [round(float(c), 2) for _, c in pairs]
                return {"labels": labels, "prices": prices, "is_intraday": is_intraday}
            except Exception as e:
                logger.debug(f"price history {symbol}: {e}")
        return None

    if not is_intraday and market == "TW":
        # TW ETF 非即時：DB → CF Proxy Yahoo（快，1 次 API）→ 並行 TWSE（慢，備援）
        data = await asyncio.to_thread(_fetch_db_price_history, ticker, p)
        if not data:
            data = await asyncio.to_thread(_fetch)        # _fetch 內部已走 CF proxy
        if not data:
            data = await asyncio.to_thread(_fetch_twse_price_history, ticker, p)
    elif is_intraday and market == "TW":
        # TW ETF 即時（1D/5D）：嘗試 Yahoo；失敗則用 DB 最近資料回傳「最近交易日」走勢
        data = await asyncio.to_thread(_fetch)
        if not data:
            data = await asyncio.to_thread(_fetch_db_price_history, ticker, p)
    else:
        # US ETF（即時或歷史）：Yahoo Finance 為唯一來源；歷史失敗則 DB fallback
        data = await asyncio.to_thread(_fetch)
        if not data and not is_intraday:
            data = await asyncio.to_thread(_fetch_db_price_history, ticker, p)
    if not data:
        return safe_json({"status": "error", "message": "無法取得歷史資料"}, 400)
    response_data = safe_json({
        "status":     "success",
        "labels":     data["labels"],
        "prices":     data["prices"],
        "is_intraday": data.get("is_intraday", False),
        "is_partial":  data.get("is_partial", False),
    })
    cache.set(_hist_cache_key, response_data, _HIST_CACHE_TTL.get(p, 3600))
    return response_data


@router.get("/api/etf/history")
async def get_etf_history(ticker: str = "", period: str = "1mo"):
    """Alias → /api/etf/price-history/{ticker}（向下相容）"""
    if not ticker:
        return safe_json({"status": "error", "message": "缺少 ticker 參數"}, 400)
    # 把舊格式 period 字串統一成新 API 格式
    alias = {"1mo": "1M", "3mo": "3M", "6mo": "6M", "1y": "1Y", "3y": "3Y", "5y": "5Y", "max": "MAX"}
    p = alias.get(period.lower(), period.upper())
    return await get_price_history(ticker.upper(), p)


# ── 低檔加碼提醒查詢 ──

@router.get("/api/etf/dip-alert/{ticker}")
async def get_dip_alert(ticker: str, days_20_threshold: float = 10.0, days_60_threshold: float = 15.0):
    ticker = ticker.upper()
    with get_db() as (conn, cursor):
        cursor.execute(
            "SELECT market FROM etf_master WHERE ticker=%s", (ticker,)
        )
        r = cursor.fetchone()
        market = (r or {}).get("market", "US")
        # 取最近 65 日價格
        cursor.execute(
            "SELECT current_price FROM etf_daily_data WHERE ticker=%s ORDER BY date DESC LIMIT 65",
            (ticker,)
        )
        rows = cursor.fetchall()

    if len(rows) < 5:
        return safe_json({"status": "error", "message": "歷史資料不足（需至少 5 個交易日）"}, 400)

    prices = [float(r["current_price"]) for r in reversed(rows)]
    current_price = prices[-1]
    history = prices[:-1]

    alert = check_dip_alert(history, current_price, days_20_threshold, days_60_threshold)
    return safe_json({
        "status": "success",
        "ticker": ticker,
        "current_price": current_price,
        "data": alert,   # frontend reads .data.triggered
    })


# ── ETF 配息資料 ──

@router.get("/api/etf/dividends/{ticker}")
async def get_dividends(ticker: str):
    ticker = ticker.upper()
    with get_db() as (conn, cursor):
        cursor.execute("SELECT market FROM etf_master WHERE ticker=%s", (ticker,))
        row = cursor.fetchone()
        market = (row or {}).get("market", "US")

    yt = _yahoo_ticker(ticker, market)

    def _get_divs():
        try:
            t = yf.Ticker(yt)
            divs = t.dividends
            if divs is None or divs.empty:
                return []
            if divs.index.tz is not None:
                divs.index = divs.index.tz_localize(None)
            recent = divs[divs.index >= pd.Timestamp.now() - pd.DateOffset(years=3)]
            return [{"date": d.strftime("%Y-%m-%d"), "amount": round(float(v), 6)}
                    for d, v in recent.items()]
        except Exception:
            return []

    data = await asyncio.to_thread(_get_divs)
    return safe_json({"status": "success", "data": data})


# ── 手動強制更新 ──

@router.post("/api/etf/update/{ticker}")
async def update_one_etf(ticker: str):
    ticker = ticker.upper()

    # 先讀取 market，不預先寫入 DB（避免垃圾代碼污染資料庫）
    with get_db() as (conn, cursor):
        cursor.execute("SELECT market FROM etf_master WHERE ticker=%s", (ticker,))
        row = cursor.fetchone()
    market = (row or {}).get("market") or ("TW" if ticker[:4].isdigit() else "US")

    # 先抓取資料，確認 ticker 真實存在後才寫入 DB
    def _fetch():
        return fetch_one_etf(ticker, market)

    data = await asyncio.to_thread(_fetch)
    if not data or not data.get("current_price"):
        return safe_json({"status": "error", "message": f"無法取得 {ticker} 資料，請確認代碼是否正確"}, 400)

    data["ticker"] = ticker
    data["market"] = market
    # 確認有效資料後才 INSERT（INSERT IGNORE 確保已存在時不覆蓋）
    if not row:
        with get_db() as (conn, cursor):
            cursor.execute(
                "INSERT IGNORE INTO etf_master (ticker, name, market, auto_discovered) VALUES (%s, %s, %s, 1)",
                (ticker, data.get("name", ticker), market),
            )
            conn.commit()

    cache.delete(f"detail:{ticker}")
    save_etf_data(data)
    return safe_json({"status": "success", "message": f"{ticker} 已更新", "data": data})


@router.post("/api/etf/force-update")
async def force_update(request: Request):
    from auth import get_current_user
    from fastapi import HTTPException
    try:
        get_current_user(request, credentials=None)
    except HTTPException:
        return safe_json({"status": "error", "message": "請先登入"}, 401)
    from scheduler import schedule_update
    schedule_update()
    return safe_json({"status": "success", "message": "已觸發全量更新"})


@router.get("/api/fx/usdtwd")
async def get_usdtwd_rate():
    """公開匯率端點（無需登入），供首頁 / 公開頁面顯示 USD/TWD。"""
    from services.exchange_rate import get_usd_twd, get_fx_age_seconds
    try:
        rate = await asyncio.to_thread(get_usd_twd)
        age  = get_fx_age_seconds()
        return safe_json({
            "status":      "success",
            "rate":        round(float(rate), 4),
            "age_seconds": round(age, 0) if age is not None else None,
        })
    except Exception as e:
        return safe_json({"status": "error", "message": str(e)}, 500)


# ── ETF 評分 ──

@router.get("/api/etf/score/{ticker}")
async def get_etf_score(ticker: str):
    """ETF 綜合健康評分（0-100 + A~F 等第）。
    評分維度：報酬力 / 配息力 / 成本效率 / 穩定性 / 動能（同市場相互比較）。
    """
    ticker = ticker.upper().strip()
    from services.etf_score import score_etf
    result = await asyncio.to_thread(score_etf, ticker)
    if not result:
        return safe_json({"status": "error", "message": f"無法計算 {ticker} 評分（可能尚無資料）"}, 404)
    return safe_json({"status": "success", "data": result})


@router.get("/api/etf/scores/top")
async def get_top_scores(market: str = "TW", limit: int = 10):
    """取同市場中評分最高的 ETF 清單（首頁 / 排行榜用）。"""
    market = market.upper() if market.upper() in ("TW", "US") else "TW"
    limit  = max(5, min(limit, 30))
    cache_key = f"etf_scores_top:{market}:{limit}"
    cached = cache.get(cache_key)
    if cached:
        return safe_json({"status": "success", "data": cached})

    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT m.ticker
                FROM etf_master m
                WHERE m.is_hot=1 AND m.market=%s AND m.is_delisted=0
            """, (market,))
            tickers = [r["ticker"] for r in cursor.fetchall()]

        from services.etf_score import score_batch
        scores = await asyncio.to_thread(score_batch, tickers)
        ranked = sorted(scores.values(), key=lambda x: x["score"], reverse=True)[:limit]
        cache.set(cache_key, ranked, 1800)
        return safe_json({"status": "success", "data": ranked})
    except Exception as e:
        logger.error(f"get_top_scores error: {e}", exc_info=True)
        return safe_json({"status": "error", "message": str(e)}, 500)


# ── 新增 ETF ──

@router.post("/api/etf/add-to-master")
async def add_etf_to_master(body: EtfAddIn, request: Request):
    from auth import get_current_user
    from fastapi import HTTPException
    try:
        get_current_user(request, credentials=None)
    except HTTPException:
        return safe_json({"status": "error", "message": "請先登入"}, 401)
    with get_db() as (conn, cursor):
        cursor.execute(
            "INSERT INTO etf_master (ticker,name,market) VALUES (%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE name=VALUES(name), market=VALUES(market)",
            (body.ticker, body.name, body.market)
        )
        conn.commit()
    return safe_json({"status": "success", "message": f"ETF {body.ticker} 已加入"})
