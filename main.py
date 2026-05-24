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
from fastapi.middleware.trustedhost import TrustedHostMiddleware

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
    ‣ seed_etf_master() 已於啟動時植入 91 檔熱門 ETF，排行榜不依賴 TWSE 同步
    ‣ 每次啟動呼叫 sync_tw_etfs 會多花 30-60 秒等待 TWSE HTTP API，屬不必要成本
    """
    await asyncio.sleep(3)

    # Step 1: 更新活躍 ETF 即時行情（有資料即顯示，不必等全部完成）
    from scheduler import _update_active
    logger.info("▶ 開始更新活躍 ETF 行情...")
    await _update_active()

    # Step 3: 背景補齊 TW ETF 歷史收盤價（只補缺月，冪等；不阻塞啟動）
    async def _bg_backfill():
        await asyncio.sleep(30)  # 等 _update_active 完成後再開始，避免同時競爭 DB
        try:
            from services.twse_history import backfill_tw_history
            result = await asyncio.to_thread(backfill_tw_history)
            logger.info(f"▶ 啟動歷史補齊完成：{result['etfs']} 檔，補 {result['days_inserted']} 日")
        except Exception as e:
            logger.warning(f"啟動歷史補齊失敗（繼續）: {e}")

    asyncio.ensure_future(_bg_backfill())
    logger.info("✅ 啟動序列完成（歷史補齊已在背景啟動）")


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

# ── 安全 HTTP 標頭（防 Clickjacking / MIME-sniffing / 資訊洩漏）──
@app.middleware("http")
async def add_security_headers(request: Request, call_next: Callable) -> Response:
    response = await call_next(request)
    response.headers["X-Frame-Options"]           = "DENY"
    response.headers["X-Content-Type-Options"]    = "nosniff"
    response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"]          = "1; mode=block"
    response.headers["Permissions-Policy"]        = "geolocation=(), microphone=(), camera=()"
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, reload_excludes=["*.db"])
