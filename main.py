"""
ETF 投資管理系統 v2.0
完整重構版：JWT 驗證 + Google OAuth + bcrypt + 低檔加碼 + DRIP + 即時匯率
"""
import asyncio, logging, time
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from config import TEMPLATES_DIR, STATIC_DIR, APP_URL
from database import init_db, get_db
from etf_data import seed_etf_master, fetch_one_etf, save_etf_data
import scheduler as sched

# ── 路由模組 ──
from routes import auth_routes, etf_routes, portfolio_routes, watchlist_routes
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
    sched.start_scheduler()
    asyncio.create_task(_startup_sequence())
    yield
    logger.info("系統關閉")


async def _startup_sequence():
    """啟動後：
    1. 更新活躍標的行情（庫存 + 自選 + 熱門，3 分鐘內完成）
    2. 歷史補齊在背景運行（不阻塞排行榜顯示）

    NOTE: sync_tw_etfs 已移至每日 08:00 排程執行（不在啟動時跑）
    ‣ seed_etf_master() 已於啟動時植入 38 檔熱門 ETF，排行榜不依賴 TWSE 同步
    ‣ 每次啟動呼叫 sync_tw_etfs 會多花 30-60 秒等待 TWSE HTTP API，屬不必要成本
    """
    await asyncio.sleep(3)

    # Step 1: 更新活躍 ETF 即時行情（有資料即顯示，不必等全部完成）
    from scheduler import _update_active
    logger.info("▶ 開始更新活躍 ETF 行情...")
    await _update_active()

    # Step 3: 背景補齊 TW ETF 歷史收盤價，補齊後立即重算年化報酬率
    async def _bg_backfill():
        await asyncio.sleep(30)  # 等 _update_active 完成後再開始，避免同時競爭 DB
        try:
            from services.twse_history import backfill_tw_history
            result = await asyncio.to_thread(backfill_tw_history)
            logger.info(f"▶ 啟動 TW 歷史補齊完成：{result['etfs']} 檔，補 {result['days_inserted']} 日")
        except Exception as e:
            logger.warning(f"啟動 TW 歷史補齊失敗（繼續）: {e}")

        # Step 3b: 補齊 US ETF 歷史收盤價（透過 CF 代理，只補缺失日期，冪等）
        try:
            from services.us_history import backfill_us_history
            us_result = await asyncio.to_thread(backfill_us_history)
            logger.info(f"▶ 啟動 US 歷史補齊完成：{us_result['etfs']} 檔，補 {us_result['days_inserted']} 日")
        except Exception as e:
            logger.warning(f"啟動 US 歷史補齊失敗（繼續）: {e}")

        # Step 4: 歷史補齊後立即重算年化報酬率（讓排行榜 return tab 有資料）
        try:
            from services.returns_calc import recalc_all_returns
            ret = await asyncio.to_thread(recalc_all_returns)
            logger.info(f"▶ 啟動報酬率重算完成：更新 {ret['updated']} 檔")
            from cache import cache
            cache.delete_prefix("rank:")   # 讓下次請求取得最新報酬率排名
        except Exception as e:
            logger.warning(f"啟動報酬率重算失敗（繼續）: {e}")

    asyncio.create_task(_bg_backfill())
    logger.info("✅ 啟動序列完成（歷史補齊 + 報酬率重算已在背景啟動）")


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
app.add_middleware(
    CORSMiddleware,
    allow_origins=[APP_URL, "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 公開 API 快取策略（啟用瀏覽器 HTTP 快取，減少冗餘伺服器往返）──
# 只對 GET 請求且 2xx 回應的公開 ETF/FX 端點加 Cache-Control；
# 用戶私有資料（auth/portfolio/watchlist）不在此清單，回傳 no-store。
_API_CACHE_MAP: list[tuple[str, int]] = [
    ("/api/etf/index",          1800),   # 30 min — ETF master 清單鮮少變動
    ("/api/etf/rankings/",       300),   # 5 min  — 行情排行
    ("/api/etf/scores/top",      600),   # 10 min — 評分榜
    ("/api/etf/score/",          600),   # 10 min — 單一評分
    ("/api/etf/detail/",         300),   # 5 min  — 單一詳情
    ("/api/etf/price-history/",   60),   # 1 min  — 走勢圖（價格變動頻繁）
    ("/api/etf/dip-alert/",      120),   # 2 min  — 低檔警示
    ("/api/fx/",                 300),   # 5 min  — 匯率
]

@app.middleware("http")
async def add_cache_control(request: Request, call_next: Callable) -> Response:
    response = await call_next(request)
    if request.method == "GET" and response.status_code < 300:
        path = request.url.path
        for prefix, ttl in _API_CACHE_MAP:
            if path.startswith(prefix):
                # stale-while-revalidate = 2×ttl：過期後背景重驗證，前景不等待
                response.headers.setdefault(
                    "Cache-Control",
                    f"public, max-age={ttl}, stale-while-revalidate={ttl * 2}"
                )
                break
    return response


# ── 安全 HTTP 標頭（防 Clickjacking / MIME-sniffing / 資訊洩漏）──
@app.middleware("http")
async def add_security_headers(request: Request, call_next: Callable) -> Response:
    response = await call_next(request)
    response.headers["X-Frame-Options"]           = "DENY"
    response.headers["X-Content-Type-Options"]    = "nosniff"
    response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"]          = "1; mode=block"
    response.headers["Permissions-Policy"]        = "geolocation=(), microphone=(), camera=()"
    # CSP：允許已知 CDN（Tailwind/Chart.js/FontAwesome）與 Google OAuth；
    # 因模板使用大量 inline script/style，需保留 unsafe-inline，
    # 但仍透過限制 connect-src / img-src / object-src 縮小攻擊面。
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
            "https://cdn.tailwindcss.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "font-src 'self' https://cdnjs.cloudflare.com; "
        "img-src 'self' data: https://lh3.googleusercontent.com; "
        "connect-src 'self'; "
        "frame-src 'none'; "
        "object-src 'none';"
    )
    return response

# ── 靜態檔案 ──
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── 模板 ──
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# 注入 templates 到各路由模組
for mod in [auth_routes, etf_routes, portfolio_routes, watchlist_routes,
            user_routes, backtest_routes, notification_routes]:
    if hasattr(mod, "templates"):
        mod.templates = templates

# ── 路由注冊 ──
app.include_router(auth_routes.router,         tags=["Auth"])
app.include_router(etf_routes.router,          tags=["ETF"])
app.include_router(portfolio_routes.router,    tags=["Portfolio"])
app.include_router(watchlist_routes.router,    tags=["Watchlist"])
app.include_router(user_routes.router,         tags=["User"])
app.include_router(backtest_routes.router,     tags=["Backtest"])
app.include_router(notification_routes.router, tags=["Notifications"])


# ── 健康檢查（供 Load Balancer / 監控使用）──
@app.get("/health")
async def health_check():
    """回傳系統健康狀態，包含資料庫連線與快取狀態。"""
    from utils import safe_json
    checks: dict = {"status": "ok", "db": "ok", "timestamp": int(time.time())}
    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT 1")
    except Exception as e:
        checks["db"] = f"error: {e}"
        checks["status"] = "degraded"
    return safe_json(checks)


@app.get("/health/detail")
async def health_detail():
    """詳細健康狀態：顯示活躍 ETF 的資料新鮮度，方便發現「悄悄壞掉」的問題。

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
    from utils import safe_json

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

    except Exception as e:
        return safe_json({
            "status": "degraded",
            "checked_at": today.isoformat(),
            "db": f"error: {e}",
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

    return safe_json({
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
    })


@app.get("/api/health/data")
async def health_data():
    """資料完整性健康檢查。

    檢查項目：
    1. 缺漏 ETF   — 在 etf_master 但 etf_daily_data 完全無資料（從未抓到）
    2. 過期資料   — 有資料但超過 3 天未更新（可能抓取中斷）
    3. 欄位異常   — annual_return_1y / dividend_yield 全為 NULL（資料品質不足）
    4. 重複代碼   — etf_master 中同 ticker 出現多筆（schema 有 PK 但仍記錄）
    5. 市場標記錯誤 — ticker 前 4 碼全為數字但 market != 'TW'（或反之）
    6. 快取狀態   — rank:combined:TW / rank:combined:US 是否有效
    7. 配息事件   — etf_dividends 中有真實事件的 ETF 數量（回測品質指標）
    """
    from datetime import date as _date
    from utils import safe_json
    from cache import cache

    today = _date.today()
    issues: list[dict] = []
    summary: dict = {}

    try:
        with get_db() as (conn, cursor):

            # 1. 缺漏 ETF（in master but no price data at all）
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
                HAVING MAX(d.date) < DATE_SUB(CURDATE(), INTERVAL 3 DAY)
                ORDER BY last_date ASC
                LIMIT 50
            """)
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

            # 4. 市場標記錯誤（TW 代碼但 market=US，或反之）
            cursor.execute("""
                SELECT ticker, market
                FROM etf_master
                WHERE is_delisted = 0 AND (
                    (CHAR_LENGTH(ticker) >= 4
                     AND SUBSTRING(ticker,1,1) REGEXP '^[0-9]$'
                     AND SUBSTRING(ticker,4,1) REGEXP '^[0-9]$'
                     AND market != 'TW')
                    OR
                    (ticker REGEXP '^[A-Z]{2,5}$' AND market != 'US')
                )
                ORDER BY ticker
            """)
            wrong_market = cursor.fetchall()
            summary["wrong_market_etfs"] = len(wrong_market)
            for r in wrong_market:
                issues.append({"type": "wrong_market", "ticker": r["ticker"],
                                "market": r["market"],
                                "detail": f"代碼形式與 market={r['market']} 不符"})

            # 5. 異常價格：current_price ≤ 0（包含被零值覆蓋的紀錄）
            cursor.execute("""
                SELECT ticker, date, current_price
                FROM etf_daily_data
                WHERE current_price <= 0 OR current_price IS NULL
                ORDER BY date DESC
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
                SELECT t1.ticker,
                       t1.current_price AS today_price,
                       t2.current_price AS prev_price,
                       ROUND(ABS(t1.current_price - t2.current_price)
                             / t2.current_price * 100, 1) AS pct_diff
                FROM etf_daily_data t1
                JOIN etf_daily_data t2 ON t1.ticker = t2.ticker
                JOIN etf_master m ON m.ticker = t1.ticker
                WHERE t1.date = (
                    SELECT MAX(d1.date) FROM etf_daily_data d1
                    WHERE d1.ticker = t1.ticker AND d1.current_price > 0
                )
                  AND t2.date = (
                    SELECT MAX(d2.date) FROM etf_daily_data d2
                    WHERE d2.ticker = t1.ticker AND d2.current_price > 0
                      AND d2.date < t1.date
                )
                  AND t1.current_price > 0 AND t2.current_price > 0
                  AND ABS(t1.current_price - t2.current_price) / t2.current_price > 0.30
                  AND m.is_hot = 1 AND m.is_delisted = 0
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
                HAVING ABS(portfolio_shares - tx_net) > 0.001
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

    except Exception as e:
        return safe_json({"status": "error", "detail": str(e)}, 500)

    # 快取狀態（不需 DB）
    cache_status = {
        "rank_tw":  "hit" if cache.get("rank:combined:TW") else "miss",
        "rank_us":  "hit" if cache.get("rank:combined:US") else "miss",
        "etf_index": "hit" if cache.get("etf:index") else "miss",
    }
    summary["cache"] = cache_status

    # FX 匯率新鮮度
    try:
        from services.exchange_rate import get_fx_age_seconds
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
    if "fx_stale" in [i["type"] for i in issues]:
        warning_count += 1
    if critical_count > 0:
        overall = "degraded"
    elif warning_count > 5:
        overall = "warning"
    else:
        overall = "ok"

    return safe_json({
        "status":      overall,
        "checked_at":  today.isoformat(),
        "summary":     summary,
        "issues":      issues,
        "issue_count": len(issues),
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, reload_excludes=["*.db"])
