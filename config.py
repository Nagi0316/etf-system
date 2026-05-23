"""
config.py — 全域設定，從 .env 載入
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# ── 載入 .env ──
_env = BASE_DIR / ".env"
if _env.exists():
    with open(_env, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

# ── 資料庫 ──
DB_HOST     = os.getenv("DB_HOST", "")
DB_PORT     = int(os.getenv("DB_PORT", "4000"))
DB_USER     = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME     = os.getenv("DB_NAME", "etf_tracker")
SQLITE_PATH = str(BASE_DIR / "etf_tracker.db")
USE_MYSQL   = bool(DB_HOST and DB_USER and DB_PASSWORD)

# ── JWT ──
_raw_jwt_secret = os.getenv("JWT_SECRET", "")
if not _raw_jwt_secret:
    if os.getenv("ENV", "").lower() == "production":
        raise RuntimeError(
            "JWT_SECRET 環境變數未設定。正式環境必須設定此變數，"
            "請在 .env 或系統環境變數中設定一個高熵隨機字串（例如：openssl rand -hex 32）。"
        )
    import logging as _log
    _log.getLogger("config").warning(
        "⚠️  JWT_SECRET 未設定，使用固定開發 Secret。切勿在正式環境使用此設定！"
    )
    _raw_jwt_secret = "dev-only-fixed-secret-do-not-use-in-production-32chars"
JWT_SECRET       = _raw_jwt_secret
JWT_ALGORITHM    = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "168"))  # 7 days

# ── Google OAuth ──
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/auth/google/callback")

# ── App ──
APP_URL       = os.getenv("APP_URL", "http://localhost:8000")
TEMPLATES_DIR = str(BASE_DIR / "templates")
STATIC_DIR    = str(BASE_DIR / "static")
AVATAR_DIR    = str(BASE_DIR / "static" / "uploads" / "avatars")

# ── 通知 ──
LINE_NOTIFY_TOKEN = os.getenv("LINE_NOTIFY_TOKEN", "")
SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

# ── 目錄建立 ──
for _d in [TEMPLATES_DIR, AVATAR_DIR, str(BASE_DIR / "static" / "css")]:
    os.makedirs(_d, exist_ok=True)
