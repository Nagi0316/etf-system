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
    1. 同步 TWSE 全市場台股 ETF 代碼（非阻塞）
    2. 用「用戶驅動池」更新活躍標的（庫存 + 自選 + 熱門）
    """
    await asyncio.sleep(3)

    # Step 1: 先同步台股全市場代碼建檔
    try:
        from services.twse_sync import sync_tw_etfs
        new_count = await asyncio.to_thread(sync_tw_etfs)
        logger.info(f"▶ 啟動 TWSE 同步完成，新增 {new_count} 檔台股 ETF 基本資料")
    except Exception as e:
        logger.warning(f"啟動 TWSE 同步失敗（繼續）: {e}")

    # Step 2: 用用戶驅動池更新即時行情
    from scheduler import _update_active
    logger.info("▶ 開始更新活躍 ETF 行情...")
    await _update_active()
    logger.info("✅ 啟動序列完成")


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, reload_excludes=["*.db"])
