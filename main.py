"""
ETF 投資管理系統 v2.0
完整重構版：JWT 驗證 + Google OAuth + bcrypt + 低檔加碼 + DRIP + 即時匯率
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from application_config import ASSET_VERSION, TEMPLATES_DIR, STATIC_DIR, APP_URL
from database import init_db, get_db
from etf_market_data import seed_etf_master
import market_data_scheduler as sched

# ── 路由模組 ──
from routes import authentication_routes, etf_routes, portfolio_routes, watchlist_routes
from routes import user_routes, backtest_routes, notification_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
#  Lifespan
# ══════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    sched.set_loop(loop)
    init_db()
    seed_etf_master()
    scheduler_instance = sched.start_scheduler()
    startup_task = asyncio.create_task(_startup_sequence())
    try:
        yield
    finally:
        startup_task.cancel()
        await asyncio.gather(startup_task, return_exceptions=True)
        scheduler_instance.shutdown(wait=False)
        logger.info("系統關閉")


async def _startup_sequence():
    """啟動後：
    1. 立即批量同步系統內全部 ETF 的最新可用行情
    2. 再同步商品名錄並修復舊日期
    3. 歷史補齊與報酬率重算由固定排程執行

    行情優先於較慢的商品名錄同步，避免部署後數分鐘仍顯示舊價格。
    每日 08:00 排程仍會再次同步，補上新上市、下市及受益人數變化。
    """
    await asyncio.sleep(3)

    # 匯率與行情互不依賴；平行預熱，避免部署後健康檢查短暫誤報
    #「匯率從未取得」，也不延後主要行情同步。
    from services.exchange_rate_service import get_usd_twd
    fx_task = asyncio.create_task(asyncio.to_thread(get_usd_twd))

    # Step 0: 先修復所有有效 ETF 行情。商品名錄與歷史清理可能耗時數分鐘，
    # 不應阻擋使用者最先看到的價格、漲跌與成交量。
    from market_data_scheduler import _fast_price_tick
    logger.info("▶ 優先同步全部 ETF 最新行情...")
    await _fast_price_tick(force_all_markets=True)
    await asyncio.gather(fx_task, return_exceptions=True)

    # 配息是低頻資料，獨立分批補齊，不阻塞行情與頁面啟動。
    from market_data_scheduler import _update_dividend_gaps
    asyncio.create_task(_update_dividend_gaps())

    # Step 1: 同步官方 ETF 商品範圍、資產規模與受益人數。
    try:
        from services.taiwan_catalog_sync_service import sync_tw_etfs
        synced = await asyncio.to_thread(sync_tw_etfs)
        logger.info(f"▶ 啟動 ETF 商品同步完成：新增 {synced} 檔")
    except Exception as e:
        logger.warning(f"啟動 ETF 商品同步失敗（沿用既有清單）: {e}")

    # Step 2: 商品清單完成後再修復舊版誤寫的週末／未來日期；不可放在
    # init_db，避免 TiDB 遠端合併阻塞 Railway 啟動健康檢查。
    from database import _repair_non_trading_daily_rows
    await asyncio.to_thread(_repair_non_trading_daily_rows)

    # 歷史補齊與報酬率重算交由每日固定排程。部署啟動時不再立刻掃描
    # 五年歷史，避免 9 萬筆以上資料查詢與外部請求和首頁流量搶資源。
    logger.info("✅ 啟動序列完成（歷史補齊與報酬率重算由每日排程執行）")


# ══════════════════════════════════════════════════════════
#  FastAPI App
# ══════════════════════════════════════════════════════════

app = FastAPI(
    title="ETF 投資管理系統",
    version="2.0.0",
    description="支援 Google OAuth | JWT | 低檔加碼 | DRIP | 即時匯率",
    lifespan=lifespan,
)

# ── 中介軟體 ──
# GZip 壓縮：所有 ≥ 500 bytes 的回應自動壓縮，JSON 通常縮小 60-80%
app.add_middleware(GZipMiddleware, minimum_size=500)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys([
        APP_URL,
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ])),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ── 安全 HTTP 標頭（防 Clickjacking / MIME-sniffing / 資訊洩漏）──
@app.middleware("http")
async def add_security_headers(request: Request, call_next: Callable) -> Response:
    response = await call_next(request)
    response.headers["X-Frame-Options"]           = "DENY"
    response.headers["X-Content-Type-Options"]    = "nosniff"
    response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"]          = "0"
    response.headers["Permissions-Policy"]        = "geolocation=(), microphone=(), camera=()"
    if APP_URL.startswith("https://"):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # CSP：僅允許本機腳本與 Font Awesome 樣式／字型 CDN；
    # 因模板使用大量 inline script/style，需保留 unsafe-inline，
    # 但仍透過限制 connect-src / img-src / object-src 縮小攻擊面。
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "font-src 'self' https://cdnjs.cloudflare.com; "
        "img-src 'self' data: https://lh3.googleusercontent.com; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "frame-src 'none'; "
        "object-src 'none';"
    )
    # 靜態資源長期快取（版本號由檔名控制）
    path = request.url.path
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response

# ── 靜態檔案 ──
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── 模板 ──
templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["asset_version"] = ASSET_VERSION

# 注入 templates 到各路由模組
for mod in [authentication_routes, etf_routes, portfolio_routes, watchlist_routes,
            user_routes, backtest_routes, notification_routes]:
    if hasattr(mod, "templates"):
        mod.templates = templates

# ── 路由注冊 ──
app.include_router(authentication_routes.router, tags=["Auth"])
app.include_router(etf_routes.router,          tags=["ETF"])
app.include_router(portfolio_routes.router,    tags=["Portfolio"])
app.include_router(watchlist_routes.router,    tags=["Watchlist"])
app.include_router(user_routes.router,         tags=["User"])
app.include_router(backtest_routes.router,     tags=["Backtest"])
app.include_router(notification_routes.router, tags=["Notifications"])


# ── 健康檢查（供 Load Balancer / 監控使用）──
@app.get("/health")
def health_check():
    """回傳系統健康狀態，包含資料庫連線與快取狀態。"""
    from serialization_utils import safe_json
    checks: dict = {"status": "ok", "db": "ok", "timestamp": int(time.time())}
    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT 1")
    except Exception:
        logger.exception("Basic health check database query failed")
        checks["db"] = "error"
        checks["status"] = "degraded"
    return safe_json(checks)


@app.get("/health/detail")
def health_detail():
    """詳細健康狀態：顯示活躍 ETF 的資料新鮮度，方便發現「悄悄壞掉」的問題。快取 60 秒。

    staleness 分級：
      fresh      — 最後資料日距今 ≤ 1 天（正常）
      stale      — 2–3 天（可能是假日，觀察）
      very_stale — 4–7 天（需注意）
      critical   — > 7 天或完全沒資料（需要處理）

    整體 status：
      ok       — 全部 fresh 或 stale（週末合理範圍）
      warning  — 有 very_stale
      degraded — 有 critical 或 DB 異常
    """
    from datetime import date as _date
    from serialization_utils import safe_json
    from memory_cache import cache as _cache

    _hd_cached = _cache.get("health:detail")
    if _hd_cached:
        return safe_json(_hd_cached)

    today = _date.today()
    db_ok = True

    try:
        with get_db() as (conn, cursor):
            # 活躍池：熱門 + 用戶自選 + 用戶庫存（排除已下市）
            cursor.execute("""
                SELECT DISTINCT m.ticker, m.market
                FROM etf_master m
                WHERE m.is_delisted = 0
                  AND (
                    m.is_hot = 1
                    OR m.ticker IN (
                        SELECT DISTINCT ticker FROM user_watchlist
                        UNION
                        SELECT DISTINCT ticker FROM user_portfolio WHERE shares > 0
                    )
                  )
                ORDER BY m.ticker
            """)
            pool = cursor.fetchall()

            if not pool:
                return safe_json({
                    "status": "ok",
                    "checked_at": today.isoformat(),
                    "note": "活躍池為空（尚無熱門/自選/庫存 ETF）",
                    "etfs": [],
                })

            tickers = [r["ticker"] for r in pool]
            fmt = ",".join(["%s"] * len(tickers))

            # 每個 ticker 最新的資料日期
            cursor.execute(
                f"SELECT ticker, MAX(date) AS last_date "
                f"FROM etf_daily_data WHERE ticker IN ({fmt}) GROUP BY ticker",
                tickers,
            )
            date_map = {r["ticker"]: r["last_date"] for r in cursor.fetchall()}

    except Exception:
        logger.exception("Detailed health check database query failed")
        return safe_json({
            "status": "degraded",
            "checked_at": today.isoformat(),
            "db": "error",
            "etfs": [],
        }, 500)

    # 計算新鮮度
    etf_rows = []
    counts = {"fresh": 0, "stale": 0, "very_stale": 0, "critical": 0}

    for r in pool:
        ticker = r["ticker"]
        last_date = date_map.get(ticker)

        if last_date is None:
            days_ago = None
            level = "critical"
        else:
            if isinstance(last_date, str):
                last_date = _date.fromisoformat(last_date[:10])
            days_ago = (today - last_date).days
            if   days_ago <= 1: level = "fresh"
            elif days_ago <= 3: level = "stale"
            elif days_ago <= 7: level = "very_stale"
            else:               level = "critical"

        counts[level] += 1
        etf_rows.append({
            "ticker":    ticker,
            "market":    r["market"],
            "last_date": last_date.isoformat() if last_date else None,
            "days_ago":  days_ago,
            "status":    level,
        })

    # 整體狀態
    if counts["critical"] > 0:
        overall = "degraded"
    elif counts["very_stale"] > 0:
        overall = "warning"
    else:
        overall = "ok"

    # 需要注意的清單（按嚴重程度排序）
    level_order = {"critical": 0, "very_stale": 1, "stale": 2, "fresh": 3}
    etf_rows.sort(key=lambda x: (level_order[x["status"]], x["ticker"]))

    needs_attention = [
        e["ticker"] for e in etf_rows
        if e["status"] in ("critical", "very_stale")
    ]

    most_recent = max(
        (e["last_date"] for e in etf_rows if e["last_date"]),
        default=None,
    )

    payload = {
        "status":          overall,
        "checked_at":      today.isoformat(),
        "db":              "ok",
        "summary": {
            "total":      len(pool),
            "fresh":      counts["fresh"],
            "stale":      counts["stale"],
            "very_stale": counts["very_stale"],
            "critical":   counts["critical"],
        },
        "last_update":     most_recent,
        "needs_attention": needs_attention,
        "etfs":            etf_rows,
    }
    _cache.set("health:detail", payload, 60)
    return safe_json(payload)


@app.get("/api/health/data")
def health_data():
    """資料完整性健康檢查（快取 5 分鐘，避免監控頻繁輪詢打爆 DB）。

    檢查項目：
    1. 缺漏 ETF   — 在 etf_master 但 etf_daily_data 完全無資料（從未抓到）
    2. 過期資料   — 有資料但超過 3 天未更新（可能抓取中斷）
    3. 欄位異常   — annual_return_1y / dividend_yield 全為 NULL（資料品質不足）
    4. 重複代碼   — etf_master 中同 ticker 出現多筆（schema 有 PK 但仍記錄）
    5. 市場標記錯誤 — ticker 前 4 碼全為數字但 market != 'TW'（或反之）
    6. 快取狀態   — rank:combined:TW / rank:combined:US 是否有效
    7. 配息事件   — etf_dividends 中有真實事件的 ETF 數量（回測品質指標）
    """
    from datetime import date as _date, timedelta as _td
    from serialization_utils import safe_json
    from memory_cache import cache

    # ── 5 分鐘快取：完整稽核很重；即時存活狀態另由 /health 提供 ──
    _cached = cache.get("health:data")
    if _cached:
        return safe_json(_cached)

    today = _date.today()

    def _business_days_before(day, count):
        candidate = day
        remaining = count
        while remaining:
            candidate -= _td(days=1)
            if candidate.weekday() < 5:
                remaining -= 1
        return candidate

    # 以各市場最近應有交易日往前推三個工作日，避免週末造成美股假性過期。
    from routes.etf_routes import _latest_expected_quote_day
    stale_cutoffs = {
        market: _business_days_before(_latest_expected_quote_day(market), 3).isoformat()
        for market in ("TW", "US")
    }
    issues: list[dict] = []
    summary: dict = {}

    try:
        with get_db() as (conn, cursor):

            # 0. 全名錄覆蓋率：不能只檢查熱門池，否則搜尋可見的冷門 ETF
            # 即使完全沒有報價也不會被健康檢查發現。
            cursor.execute("""
                SELECT m.market,
                       COUNT(*) AS catalog,
                       SUM(CASE WHEN d.ticker IS NOT NULL THEN 1 ELSE 0 END) AS with_quote,
                       SUM(CASE WHEN d.ticker IS NULL THEN 1 ELSE 0 END) AS without_quote,
                       MIN(d.last_date) AS oldest_latest_quote,
                       MAX(d.last_date) AS newest_latest_quote
                FROM etf_master m
                LEFT JOIN (
                    SELECT ticker, MAX(date) AS last_date
                    FROM etf_daily_data
                    WHERE current_price > 0
                    GROUP BY ticker
                ) d ON d.ticker=m.ticker
                WHERE m.is_delisted=0
                GROUP BY m.market
                ORDER BY m.market
            """)
            coverage_rows = cursor.fetchall()
            summary["catalog_coverage"] = {
                r["market"]: {
                    "catalog": int(r["catalog"] or 0),
                    "with_quote": int(r["with_quote"] or 0),
                    "without_quote": int(r["without_quote"] or 0),
                    "coverage_pct": round(
                        int(r["with_quote"] or 0) / int(r["catalog"] or 1) * 100, 2
                    ),
                    "oldest_latest_quote": (
                        str(r["oldest_latest_quote"])[:10]
                        if r["oldest_latest_quote"] else None
                    ),
                    "newest_latest_quote": (
                        str(r["newest_latest_quote"])[:10]
                        if r["newest_latest_quote"] else None
                    ),
                }
                for r in coverage_rows
            }

            # 資料管線狀態：區分「資料值未變」與「同步根本失敗」。
            cursor.execute("""
                SELECT dataset, market, expected_count, received_count,
                       data_date, status, detail, last_attempt_at, last_success_at
                FROM data_sync_state
                ORDER BY dataset, market
            """)
            sync_rows = cursor.fetchall()
            summary["sync_state"] = {
                f"{r['dataset']}:{r['market']}": {
                    "status": r["status"],
                    "expected": int(r["expected_count"] or 0),
                    "received": int(r["received_count"] or 0),
                    "coverage_pct": round(
                        int(r["received_count"] or 0)
                        / int(r["expected_count"] or 1) * 100, 2
                    ),
                    "data_date": str(r["data_date"])[:10] if r["data_date"] else None,
                    "detail": r["detail"] or "",
                    "last_attempt_at": str(r["last_attempt_at"]),
                    "last_success_at": (
                        str(r["last_success_at"]) if r["last_success_at"] else None
                    ),
                }
                for r in sync_rows
            }
            for r in sync_rows:
                if r["status"] != "success":
                    issues.append({
                        "type": "sync_incomplete",
                        "market": r["market"],
                        "detail": (
                            f"{r['dataset']} 同步 {r['status']}："
                            f"{r['received_count']}/{r['expected_count']}"
                        ),
                    })

            cursor.execute("""
                SELECT
                  m.market,
                  SUM(CASE WHEN COALESCE(d.dividend_status,
                    CASE WHEN d.dividend_yield>0 THEN 'confirmed'
                         ELSE 'unknown' END)='confirmed' THEN 1 ELSE 0 END) AS confirmed,
                  SUM(CASE WHEN d.dividend_status='estimated' THEN 1 ELSE 0 END) AS estimated,
                  SUM(CASE WHEN d.dividend_status='not_applicable' THEN 1 ELSE 0 END) AS not_applicable,
                  SUM(CASE WHEN COALESCE(d.dividend_status,
                    CASE WHEN d.dividend_yield>0 THEN 'confirmed'
                         ELSE 'unknown' END)='unknown' THEN 1 ELSE 0 END) AS unknown
                FROM etf_master m
                JOIN (
                  SELECT d1.* FROM etf_daily_data d1
                  INNER JOIN (
                    SELECT ticker, MAX(date) md FROM etf_daily_data
                    WHERE current_price>0 GROUP BY ticker
                  ) d2 ON d1.ticker=d2.ticker AND d1.date=d2.md
                ) d ON d.ticker=m.ticker
                WHERE m.is_delisted=0
                GROUP BY m.market
                ORDER BY m.market
            """)
            dividend_rows = cursor.fetchall()
            dividend_keys = (
                "confirmed", "estimated", "not_applicable", "unknown"
            )
            summary["dividend_quality_by_market"] = {
                row["market"]: {
                    key: int(row.get(key) or 0) for key in dividend_keys
                }
                for row in dividend_rows
            }
            summary["dividend_quality"] = {
                key: sum(
                    int(row.get(key) or 0) for row in dividend_rows
                )
                for key in dividend_keys
            }
            cursor.execute("""
                SELECT COUNT(*) AS attempted,
                       SUM(CASE WHEN last_status='unknown' THEN 1 ELSE 0 END)
                         AS retryable
                FROM dividend_sync_state
            """)
            dividend_sync = cursor.fetchone() or {}
            summary["dividend_sync"] = {
                "attempted": int(dividend_sync.get("attempted") or 0),
                "retryable": int(dividend_sync.get("retryable") or 0),
            }

            # 1. 熱門池缺漏 ETF（會直接影響首頁與排行榜）
            cursor.execute("""
                SELECT m.ticker, m.market
                FROM etf_master m
                WHERE m.is_hot = 1 AND m.is_delisted = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM etf_daily_data d
                      WHERE d.ticker = m.ticker AND d.current_price > 0
                  )
                ORDER BY m.ticker
            """)
            missing = cursor.fetchall()
            summary["missing_etfs"] = len(missing)
            for r in missing:
                issues.append({"type": "missing", "ticker": r["ticker"],
                                "market": r["market"], "detail": "etf_master 有此代碼但無任何價格資料"})

            # 2. 過期資料（last date > 3 days ago for hot ETFs）
            cursor.execute("""
                SELECT m.ticker, m.market, MAX(d.date) AS last_date
                FROM etf_master m
                JOIN etf_daily_data d ON m.ticker = d.ticker AND d.current_price > 0
                WHERE m.is_hot = 1 AND m.is_delisted = 0
                GROUP BY m.ticker, m.market
                HAVING MAX(d.date) < CASE
                    WHEN m.market = 'TW' THEN %s
                    ELSE %s
                END
                ORDER BY last_date ASC
                LIMIT 50
            """, (stale_cutoffs["TW"], stale_cutoffs["US"]))
            stale = cursor.fetchall()
            summary["stale_etfs"] = len(stale)
            for r in stale:
                last = str(r["last_date"])[:10] if r["last_date"] else "—"
                issues.append({"type": "stale", "ticker": r["ticker"],
                                "market": r["market"], "detail": f"最後資料日：{last}"})

            # 3. 欄位異常：annual_return 與 dividend_yield 全為 NULL
            cursor.execute("""
                SELECT m.ticker, m.market,
                       d.annual_return_1y, d.dividend_yield
                FROM etf_master m
                LEFT JOIN (
                    SELECT d1.* FROM etf_daily_data d1
                    INNER JOIN (
                        SELECT ticker, MAX(date) AS md FROM etf_daily_data
                        WHERE current_price > 0 GROUP BY ticker
                    ) d2 ON d1.ticker=d2.ticker AND d1.date=d2.md
                ) d ON m.ticker = d.ticker
                WHERE m.is_hot = 1 AND m.is_delisted = 0
                  AND d.current_price IS NOT NULL AND d.current_price > 0
                  AND d.annual_return_1y IS NULL
                  AND (d.dividend_yield IS NULL OR d.dividend_yield = 0)
                ORDER BY m.ticker
            """)
            null_fields = cursor.fetchall()
            summary["null_fields_etfs"] = len(null_fields)
            for r in null_fields:
                issues.append({"type": "null_fields", "ticker": r["ticker"],
                                "market": r["market"],
                                "detail": "annual_return_1y=NULL 且 dividend_yield=0/NULL"})

            # 4. 市場標記錯誤（以 Python 判斷，避免 MySQL REGEXP/
            # CHAR_LENGTH 讓 SQLite 開發環境的健康檢查失效）
            cursor.execute("""
                SELECT ticker, market
                FROM etf_master
                WHERE is_delisted = 0
                ORDER BY ticker
            """)
            wrong_market = []
            for row in cursor.fetchall():
                ticker = str(row["ticker"] or "")
                looks_tw = (
                    len(ticker) >= 4
                    and ticker[0].isdigit()
                    and ticker[3].isdigit()
                )
                looks_us = (
                    2 <= len(ticker) <= 5
                    and ticker.isalpha()
                    and ticker.isupper()
                )
                if (
                    (looks_tw and row["market"] != "TW")
                    or (looks_us and row["market"] != "US")
                ):
                    wrong_market.append(row)
            summary["wrong_market_etfs"] = len(wrong_market)
            for r in wrong_market:
                issues.append({"type": "wrong_market", "ticker": r["ticker"],
                                "market": r["market"],
                                "detail": f"代碼形式與 market={r['market']} 不符"})

            # 5. 異常價格：current_price ≤ 0（包含被零值覆蓋的紀錄）
            cursor.execute("""
                SELECT d.ticker, d.date, d.current_price
                FROM etf_daily_data d
                JOIN etf_master m ON m.ticker = d.ticker
                WHERE m.is_delisted = 0
                  AND (d.current_price <= 0 OR d.current_price IS NULL)
                ORDER BY d.date DESC
                LIMIT 20
            """)
            bad_prices = cursor.fetchall()
            summary["bad_price_rows"] = len(bad_prices)
            for r in bad_prices:
                issues.append({"type": "bad_price", "ticker": r["ticker"],
                                "detail": f"date={str(r['date'])[:10]} price={r['current_price']}"})

            # 6. 資料完整性：user_portfolio 中出現負庫存（交易邏輯異常）
            cursor.execute("""
                SELECT user_id, ticker, shares
                FROM user_portfolio
                WHERE shares < 0
                ORDER BY shares ASC
                LIMIT 20
            """)
            neg_shares = cursor.fetchall()
            summary["negative_shares_count"] = len(neg_shares)
            for r in neg_shares:
                issues.append({"type": "negative_shares",
                                "ticker": r["ticker"],
                                "detail": f"user_id={r['user_id']} shares={r['shares']}"})

            # 7. 異常價格波動：熱門 ETF 當日收盤與前一交易日相差 > 30%（可能爬蟲抓到錯誤價格）
            cursor.execute("""
                SELECT ticker, today_price, prev_price,
                       ROUND(ABS(today_price - prev_price)
                             / prev_price * 100, 1) AS pct_diff
                FROM (
                    SELECT d.ticker,
                           d.current_price AS today_price,
                           LAG(d.current_price) OVER (
                               PARTITION BY d.ticker ORDER BY d.date
                           ) AS prev_price,
                           ROW_NUMBER() OVER (
                               PARTITION BY d.ticker ORDER BY d.date DESC
                           ) AS latest_row
                    FROM etf_daily_data d
                    JOIN etf_master m ON m.ticker = d.ticker
                    WHERE d.current_price > 0
                      AND m.is_hot = 1 AND m.is_delisted = 0
                ) latest
                WHERE latest_row = 1 AND prev_price > 0
                  AND ABS(today_price - prev_price) / prev_price > 0.30
                ORDER BY pct_diff DESC
                LIMIT 10
            """)
            volatile = cursor.fetchall()
            summary["volatile_price_count"] = len(volatile)
            for r in volatile:
                issues.append({"type": "volatile_price", "ticker": r["ticker"],
                                "detail": f"今日={r['today_price']} 前日={r['prev_price']} 波動={r['pct_diff']}%"})

            # 8. 統計：熱門 ETF 中有真實配息事件的比例
            cursor.execute("""
                SELECT COUNT(DISTINCT d.ticker) AS cnt
                FROM etf_dividends d
                JOIN etf_master m ON m.ticker = d.ticker
                WHERE m.is_hot = 1 AND m.is_delisted = 0
            """)
            div_row = cursor.fetchone()
            summary["etfs_with_real_dividends"] = (div_row or {}).get("cnt", 0)

            # 9. 庫存對帳：user_portfolio.shares 應等於 transactions 的 buy-sell 合計
            #    任何不符合的帳戶/代碼組合 → 資料不一致警告
            cursor.execute("""
                SELECT
                    p.user_id,
                    p.ticker,
                    p.shares                       AS portfolio_shares,
                    COALESCE(t.buy_shares,  0)     AS tx_buy,
                    COALESCE(t.sell_shares, 0)     AS tx_sell,
                    ROUND(
                        COALESCE(t.buy_shares, 0) - COALESCE(t.sell_shares, 0)
                    , 6)                           AS tx_net
                FROM user_portfolio p
                LEFT JOIN (
                    SELECT user_id, ticker,
                           SUM(CASE WHEN transaction_type='buy'  THEN shares ELSE 0 END) AS buy_shares,
                           SUM(CASE WHEN transaction_type='sell' THEN shares ELSE 0 END) AS sell_shares
                    FROM user_transactions
                    GROUP BY user_id, ticker
                ) t ON p.user_id = t.user_id AND p.ticker = t.ticker
                WHERE ABS(
                    p.shares - (
                        COALESCE(t.buy_shares, 0)
                        - COALESCE(t.sell_shares, 0)
                    )
                ) > 0.001
                ORDER BY p.user_id, p.ticker
                LIMIT 50
            """)
            drift_rows = cursor.fetchall()
            summary["portfolio_drift_count"] = len(drift_rows)
            for r in drift_rows:
                issues.append({
                    "type":   "portfolio_drift",
                    "ticker": r["ticker"],
                    "detail": (
                        f"user_id={r['user_id']} "
                        f"portfolio={r['portfolio_shares']} "
                        f"tx_net={r['tx_net']} "
                        f"(buy={r['tx_buy']} sell={r['tx_sell']})"
                    ),
                })

            # 熱門 ETF 總數
            cursor.execute("SELECT COUNT(*) AS cnt FROM etf_master WHERE is_hot=1 AND is_delisted=0")
            hot_cnt = (cursor.fetchone() or {}).get("cnt", 0)
            summary["hot_etfs_total"] = hot_cnt

    except Exception:
        logger.exception("Full data health audit failed")
        return safe_json({"status": "error", "detail": "資料健康檢查暫時無法完成"}, 500)

    # 快取狀態（不需 DB）
    cache_status = {
        "rank_tw":  "hit" if cache.get("rank:combined:TW") else "miss",
        "rank_us":  "hit" if cache.get("rank:combined:US") else "miss",
        "etf_index": "hit" if cache.get("etf:index") else "miss",
    }
    summary["cache"] = cache_status

    # FX 匯率新鮮度
    try:
        from services.exchange_rate_service import get_fx_age_seconds
        fx_age = get_fx_age_seconds()
        if fx_age is None:
            fx_status = "never_fetched"
            issues.append({"type": "fx_stale", "detail": "匯率從未成功取得（系統啟動後尚無成功記錄）"})
        elif fx_age > 3600:
            fx_status = f"stale_{int(fx_age)}s"
            issues.append({"type": "fx_stale",
                           "detail": f"匯率已逾 {int(fx_age//60)} 分鐘未更新（所有來源可能失敗）"})
        else:
            fx_status = f"ok_{int(fx_age)}s"
        summary["fx_age_seconds"] = round(fx_age, 1) if fx_age is not None else None
        summary["fx_status"] = fx_status
    except Exception:
        summary["fx_status"] = "unknown"

    # 整體評級
    critical_count = (summary["missing_etfs"] + summary["wrong_market_etfs"]
                      + summary.get("bad_price_rows", 0)
                      + summary.get("negative_shares_count", 0)
                      + summary.get("portfolio_drift_count", 0))
    warning_count  = (summary["stale_etfs"] + summary["null_fields_etfs"]
                      + summary.get("volatile_price_count", 0))
    warning_count += sum(1 for i in issues if i["type"] == "sync_incomplete")
    if "fx_stale" in [i["type"] for i in issues]:
        warning_count += 1
    if critical_count > 0:
        overall = "degraded"
    elif warning_count > 5:
        overall = "warning"
    else:
        overall = "ok"

    payload = {
        "status":      overall,
        "checked_at":  today.isoformat(),
        "summary":     summary,
        "issues":      issues,
        "issue_count": len(issues),
    }
    cache.set("health:data", payload, 300)
    return safe_json(payload)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, reload_excludes=["*.db"])
