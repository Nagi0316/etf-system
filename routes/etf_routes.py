"""
routes/etf_routes.py — ETF 清單、詳情、搜尋、排行榜、歷史
"""
import asyncio, logging, time
from datetime import datetime, timedelta, timezone, date
from typing import Optional
from fastapi import APIRouter, Request, Query
from fastapi.templating import Jinja2Templates

import yfinance as yf
import pandas as pd

from auth import get_optional_user
from models import EtfAddIn
from database import get_db
from utils import safe_json, safe_float
from cache import cache, CACHE_TTL_RANK, CACHE_TTL_DETAIL
import requests as _req
import certifi as _certifi

from etf_data import fetch_one_etf, save_etf_data, _yahoo_ticker, _new_session
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
        SELECT ticker, MAX(date) AS max_date FROM etf_daily_data GROUP BY ticker
    ) d2 ON d1.ticker = d2.ticker AND d1.date = d2.max_date
) d ON m.ticker = d.ticker
"""

_ETF_DETAIL_SELECT = """
    SELECT m.ticker, m.name, m.market,
        COALESCE(d.current_price,0) as current_price,
        COALESCE(d.price_change,0) as price_change,
        COALESCE(d.price_change_percent,0) as price_change_percent,
        COALESCE(d.volume,0) as volume,
        COALESCE(
            NULLIF(d.asset_size, 0),
            CASE WHEN m.outstanding_units > 0
                 THEN m.outstanding_units * COALESCE(d.current_price, 0)
                 ELSE 0 END
        ) as asset_size,
        COALESCE(d.nav,0) as nav,
        COALESCE(d.discount_premium,0) as discount_premium,
        COALESCE(d.dividend_yield,0) as dividend_yield,
        COALESCE(d.payout_freq,'不配息') as payout_freq,
        COALESCE(d.annual_return_1y,0) as annual_return_1y,
        COALESCE(d.annual_return_3y,0) as annual_return_3y,
        COALESCE(d.annual_return_5y,0) as annual_return_5y,
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

@router.get("/api/etf-rankings/{rank_type}")
async def get_etf_rankings(rank_type: str, market: str = ""):
    market = market.upper().strip() if market else ""
    if market not in ("TW", "US"):
        market = ""
    cache_key = f"rank:{rank_type}:{market}"
    cached = cache.get(cache_key)
    if cached:
        return safe_json({"status": "success", "data": cached})

    ORDER_MAP = {
        "return":   "d.annual_return_1y DESC",
        "dividend": "d.dividend_yield DESC",
        "yield":    "d.dividend_yield DESC",
        "volume":   "d.volume DESC",
        "asset":    "asset_size DESC",
        "assets":   "asset_size DESC",
        "drop":     "d.price_change_percent ASC",
        "rise":     "d.price_change_percent DESC",
    }
    order = ORDER_MAP.get(rank_type, "d.annual_return_1y DESC")
    market_filter = "AND m.market = %s" if market else ""
    params = (market,) if market else ()

    with get_db() as (conn, cursor):
        cursor.execute(f"""
            SELECT m.ticker, m.name, m.market,
                COALESCE(d.current_price,0) as current_price,
                COALESCE(d.price_change,0) as price_change,
                COALESCE(d.price_change_percent,0) as price_change_percent,
                COALESCE(d.volume,0) as volume,
                COALESCE(
                    NULLIF(d.asset_size, 0),
                    CASE WHEN m.outstanding_units > 0
                         THEN m.outstanding_units * COALESCE(d.current_price, 0)
                         ELSE 0 END
                ) as asset_size,
                COALESCE(d.dividend_yield,0) as dividend_yield,
                COALESCE(d.payout_freq,'不配息') as payout_freq,
                COALESCE(d.annual_return_1y,0) as annual_return_1y,
                COALESCE(d.expense_ratio,0) as expense_ratio
            FROM etf_master m
            {LATEST_DAILY_JOIN}
            WHERE d.current_price IS NOT NULL AND d.current_price > 0
              AND COALESCE(m.is_delisted, 0) = 0
              {market_filter}
            ORDER BY {order}
            LIMIT 10
        """, params)
        rows = cursor.fetchall()

    cache.set(cache_key, rows, CACHE_TTL_RANK)
    return safe_json({"status": "success", "data": rows})


# ── 搜尋 ──

@router.get("/api/etf/search")
async def search_etf(request: Request, q: str = Query(..., min_length=1)):
    q_up = q.upper().strip()
    cache_key = f"search:{q_up}"
    cached = cache.get(cache_key)
    if cached:
        return safe_json({"status": "success", "data": cached})

    with get_db() as (conn, cursor):
        cursor.execute(f"""
            SELECT m.ticker, m.name, m.market,
                COALESCE(d.current_price,0) as current_price,
                COALESCE(d.price_change_percent,0) as price_change_percent,
                COALESCE(d.dividend_yield,0) as dividend_yield,
                COALESCE(d.payout_freq,'不配息') as payout_freq,
                COALESCE(d.annual_return_1y,0) as annual_return_1y
            FROM etf_master m
            {LATEST_DAILY_JOIN}
            WHERE (m.ticker LIKE %s OR m.name LIKE %s)
              AND COALESCE(m.is_delisted, 0) = 0
            ORDER BY CASE WHEN m.ticker=%s THEN 0 ELSE 1 END, m.ticker
            LIMIT 30
        """, (f"{q_up}%", f"%{q}%", q_up))
        rows = cursor.fetchall()

    # ── 隨需探索：資料庫找不到且輸入看起來像代碼 ──
    if not rows and _looks_like_ticker(q_up):
        client_ip  = request.client.host if request.client else "unknown"
        discovered = await _on_demand_fetch(q_up, client_ip)
        if discovered:
            rows = [discovered]

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
                "annual_return_1y": data.get("annual_return_1y", 0),
            }
        except Exception as e:
            logger.warning(f"_on_demand_fetch {ticker}: {e}")
            return None

    return await asyncio.to_thread(_do)


@router.get("/api/etf/search/dynamic")
async def dynamic_search(request: Request, q: str = Query(..., min_length=1)):
    return await search_etf(request, q)


# ── ETF 詳情 ──

@router.get("/api/etf/detail/{ticker}")
async def get_etf_detail(ticker: str):
    ticker = ticker.upper()
    cache_key = f"detail:{ticker}"
    cached = cache.get(cache_key)
    if cached:
        return safe_json({"status": "success", "data": cached})

    usd_twd = get_usd_twd()

    with get_db() as (conn, cursor):
        row = _fetch_etf_detail_row(cursor, ticker)

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

    if row.get("market") == "US":
        row["price_twd"] = round(float(row.get("current_price", 0)) * usd_twd, 2)
        row["usd_twd_rate"] = usd_twd

    # 資料明顯過期（> 1 天）或關鍵欄位為 0，背景靜默更新一次
    _maybe_background_refresh(ticker, row)

    cache.set(cache_key, row, CACHE_TTL_DETAIL)
    return safe_json({"status": "success", "data": row})


_refresh_in_progress: set = set()
_refresh_last_ts: dict = {}      # ticker → last refresh timestamp
_REFRESH_COOLDOWN = 300          # 同一 ticker 5 分鐘內最多觸發一次

def _maybe_background_refresh(ticker: str, row: dict):
    """若資料超過 1 天或關鍵欄位缺失，在背景靜默重抓一次。"""
    import asyncio
    from datetime import date as _date
    if ticker in _refresh_in_progress:
        return
    # 冷卻：同 ticker 5 分鐘內不重複觸發
    if time.time() - _refresh_last_ts.get(ticker, 0) < _REFRESH_COOLDOWN:
        return
    data_date = row.get("data_date")
    try:
        if isinstance(data_date, str):
            data_date = datetime.strptime(data_date, "%Y-%m-%d").date()
        is_stale = (not data_date) or ((_date.today() - data_date).days >= 1)
    except Exception:
        is_stale = True
    missing_returns = (float(row.get("annual_return_1y", 0)) == 0.0 and
                       float(row.get("annual_return_3y", 0)) == 0.0)
    missing_asset   = float(row.get("asset_size", 0)) == 0.0
    if not (is_stale or missing_returns or missing_asset):
        return

    async def _do():
        _refresh_in_progress.add(ticker)
        _refresh_last_ts[ticker] = time.time()
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
        finally:
            _refresh_in_progress.discard(ticker)

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_do())
    except Exception:
        pass


# ── 歷史走勢 ──

def _fetch_db_price_history(ticker: str, period: str) -> Optional[dict]:
    """從 etf_daily_data 取歷史收盤價（Yahoo Finance 失敗時的 DB 備援）"""
    today = date.today()
    PERIOD_DAYS = {
        "1D": 1, "5D": 5, "1M": 35, "3M": 95,
        "6M": 185, "YTD": (today - date(today.year, 1, 1)).days + 1,
        "1Y": 370, "3Y": 1100, "5Y": 1830, "ALL": 9999, "MAX": 9999,
    }
    days = PERIOD_DAYS.get(period.upper(), 370)
    since = today - timedelta(days=days)
    try:
        with get_db() as (conn, cursor):
            cursor.execute(
                "SELECT date, current_price FROM etf_daily_data "
                "WHERE ticker=%s AND date >= %s AND current_price > 0 "
                "ORDER BY date ASC",
                (ticker, since.strftime("%Y-%m-%d")),
            )
            rows = cursor.fetchall()
        if len(rows) < 2:
            return None
        labels = [str(r["date"])[:10] for r in rows]
        prices = [round(float(r["current_price"]), 2) for r in rows]
        return {"labels": labels, "prices": prices, "is_intraday": False}
    except Exception as e:
        logger.debug(f"DB price history {ticker}: {e}")
    return None


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

    with get_db() as (conn, cursor):
        cursor.execute("SELECT market FROM etf_master WHERE ticker=%s", (ticker,))
        row = cursor.fetchone()
        market = (row or {}).get("market") or ("TW" if ticker[:4].isdigit() else "US")

    yt = _yahoo_ticker(ticker, market)
    # 時區偏移：台股 UTC+8，美股 EDT = UTC-4（夏令）
    tz_offset_h = 8 if market == "TW" else -4

    def _fetch():
        symbols = [yt]
        if market == "TW" and yt.endswith(".TW"):
            symbols.append(f"{ticker}.TWO")

        for symbol in symbols:
            try:
                url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
                       f"?range={yf_range}&interval={yf_interval}")
                r = _new_session(f"https://finance.yahoo.com/quote/{symbol}").get(url, timeout=15)
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
                    # 轉換為市場本地時間（UTC → 台股 UTC+8 / 美股 UTC-4）
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

    data = await asyncio.to_thread(_fetch)
    if not data and not is_intraday:
        data = await asyncio.to_thread(_fetch_db_price_history, ticker, p)
    if not data:
        return safe_json({"status": "error", "message": "無法取得歷史資料"}, 400)
    return safe_json({
        "status": "success",
        "labels": data["labels"],
        "prices": data["prices"],
        "is_intraday": data.get("is_intraday", False),
    })


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
async def force_update():
    from scheduler import schedule_update
    schedule_update()
    return safe_json({"status": "success", "message": "已觸發全量更新"})


# ── 新增 ETF ──

@router.post("/api/etf/add-to-master")
async def add_etf_to_master(body: EtfAddIn):
    with get_db() as (conn, cursor):
        cursor.execute(
            "INSERT INTO etf_master (ticker,name,market) VALUES (%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE name=VALUES(name), market=VALUES(market)",
            (body.ticker, body.name, body.market)
        )
        conn.commit()
    return safe_json({"status": "success", "message": f"ETF {body.ticker} 已加入"})
