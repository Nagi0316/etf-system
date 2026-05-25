"""
routes/auth_routes.py — 登入 / 登出 / Google OAuth / 密碼變更
所有 API 回傳 JSON；前端頁面以 template 回傳
"""
import logging, os, time
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from auth import (
    hash_password, verify_password, create_access_token,
    get_current_user, revoke_token,
    build_google_login_url, exchange_google_code, GOOGLE_CLIENT_ID
)
from models import LoginIn, RegisterIn, ChangePasswordIn
from database import get_db
from utils import safe_json

logger = logging.getLogger(__name__)
router = APIRouter()

templates: Jinja2Templates | None = None  # 由 main.py 注入

_COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 天

# ── 登入速率限制（IP 每 15 分鐘最多 10 次，防暴力破解）──
# 使用 cache 而非全域 dict，避免多 worker 下各自獨立計數失效
_RATE_WINDOW = 900   # 15 分鐘
_RATE_MAX    = 10    # 最多 10 次

def _is_login_rate_limited(ip: str) -> bool:
    from cache import cache
    key = f"rate:login:{ip}"
    now = time.time()
    timestamps: list = cache.get(key) or []
    # 只保留窗口內的時間戳
    timestamps = [t for t in timestamps if now - t < _RATE_WINDOW]
    if len(timestamps) >= _RATE_MAX:
        cache.set(key, timestamps, _RATE_WINDOW)
        return True
    timestamps.append(now)
    cache.set(key, timestamps, _RATE_WINDOW)
    return False


def _set_auth_cookies(response, token: str):
    """統一設定 HttpOnly JWT cookie 與非敏感 session 標記 cookie。
    access_token: HttpOnly + SameSite=Lax → JS 無法讀取，防 XSS 竊取
    etf_session:  非 HttpOnly → JS 僅用來判斷是否已登入，無法取得真正 token
    secure 旗標：正式環境 (ENV=production) 才開啟，防止 token 在 HTTP 明文傳送
    """
    # 預設為 secure=True（HTTPS only），僅在明確設定 ENV=development 時才關閉
    # Railway 線上環境不設此變數 → 預設 secure，更安全
    is_prod = os.getenv("ENV", "production") != "development"
    response.set_cookie(
        "access_token", token,
        httponly=True, samesite="lax", secure=is_prod,
        max_age=_COOKIE_MAX_AGE, path="/",
    )
    response.set_cookie(
        "etf_session", "1",
        httponly=False, samesite="lax", secure=is_prod,
        max_age=_COOKIE_MAX_AGE, path="/",
    )


def _clear_auth_cookies(response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("etf_session", path="/")


# ── 登入頁面 ──
@router.get("/auth", response_class=HTMLResponse)
@router.get("/login", response_class=HTMLResponse)
async def auth_page(request: Request):
    return templates.TemplateResponse("auth.html", {
        "request": request,
        "google_enabled": bool(GOOGLE_CLIENT_ID),
    })


# ══════════════════════════════════════════════════════════
#  Google OAuth
# ══════════════════════════════════════════════════════════

@router.get("/api/auth/google/login")
async def google_login(redirect_after: str = "/"):
    url = build_google_login_url(redirect_after)
    return RedirectResponse(url)


@router.get("/api/auth/google/callback")
async def google_callback(code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse(f"/auth?error={error or 'cancelled'}")
    try:
        user_info = await exchange_google_code(code, state)
        google_id  = user_info.get("sub", "")
        email      = user_info.get("email", "").lower()
        name       = user_info.get("name", email)
        picture    = user_info.get("picture", "")

        if not email:
            return RedirectResponse("/auth?error=no_email")

        # 查詢或建立使用者
        with get_db() as (conn, cursor):
            cursor.execute("SELECT id, username FROM users WHERE email=%s", (email,))
            user = cursor.fetchone()
            if user:
                uid = user["id"]
                # 更新 Google 資訊
                cursor.execute(
                    "UPDATE users SET google_id=%s, google_name=%s, google_picture=%s, auth_provider='google' WHERE id=%s",
                    (google_id, name, picture, uid)
                )
                conn.commit()
            else:
                cursor.execute(
                    "INSERT INTO users (username, email, google_id, google_name, google_picture, auth_provider) VALUES (%s,%s,%s,%s,%s,'google')",
                    (name, email, google_id, name, picture)
                )
                uid = cursor.lastrowid
                conn.commit()

        token, _ = create_access_token(uid, email)
        resp = RedirectResponse("/", status_code=302)
        _set_auth_cookies(resp, token)
        return resp

    except Exception as e:
        logger.error(f"Google OAuth 回調失敗: {e}")
        return RedirectResponse(f"/auth?error=oauth_failed")


# ══════════════════════════════════════════════════════════
#  傳統帳密（保留向下相容，但 UI 已隱藏）
# ══════════════════════════════════════════════════════════

def _get_client_ip(request: Request) -> str:
    """讀取真實客戶端 IP，優先解析 X-Forwarded-For（Railway / Cloudflare 反向代理場景）。"""
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


@router.post("/api/auth/register")
async def register(request: Request, body: RegisterIn):
    client_ip = _get_client_ip(request)
    if _is_login_rate_limited(client_ip):
        return safe_json({"status": "error", "message": "請求次數過多，請 15 分鐘後再試"}, 429)
    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT id FROM users WHERE email=%s", (body.email.lower(),))
            if cursor.fetchone():
                return safe_json({"status": "error", "message": "此信箱已被註冊"}, 400)
            cursor.execute(
                "INSERT INTO users (username, email, password_hash, auth_provider) VALUES (%s,%s,%s,'local')",
                (body.username, body.email.lower(), hash_password(body.password))
            )
            uid = cursor.lastrowid
            conn.commit()
        token, _ = create_access_token(uid, body.email.lower())
        resp = safe_json({"status": "success", "user": {"id": uid, "username": body.username}})
        _set_auth_cookies(resp, token)
        return resp
    except Exception as ex:
        logger.error(f"register: {ex}")
        return safe_json({"status": "error", "message": str(ex)}, 500)


@router.post("/api/auth/login")
async def login(request: Request, body: LoginIn):
    client_ip = _get_client_ip(request)
    if _is_login_rate_limited(client_ip):
        return safe_json({"status": "error", "message": "登入嘗試次數過多，請 15 分鐘後再試"}, 429)
    try:
        with get_db() as (conn, cursor):
            cursor.execute(
                "SELECT id, username, password_hash, avatar, google_picture FROM users WHERE email=%s",
                (body.email.lower(),)
            )
            user = cursor.fetchone()
        # 統一回傳相同錯誤訊息，防止 email 枚舉攻擊
        if not user or not user.get("password_hash"):
            return safe_json({"status": "error", "message": "信箱或密碼錯誤"}, 401)
        stored_hash = user["password_hash"]
        if not verify_password(body.password, stored_hash):
            return safe_json({"status": "error", "message": "信箱或密碼錯誤"}, 401)
        # 舊帳號若使用 SHA-256，靜默升級為 bcrypt（SHA-256 可被暴力破解）
        if len(stored_hash) == 64 and all(c in "0123456789abcdef" for c in stored_hash):
            try:
                with get_db() as (upd_conn, upd_cur):
                    upd_cur.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                                    (hash_password(body.password), user["id"]))
                    upd_conn.commit()
                logger.info("SHA-256 → bcrypt upgrade for user %s", user["id"])
            except Exception as _ue:
                logger.warning("hash upgrade failed: %s", _ue)
        token, _ = create_access_token(user["id"], body.email.lower())
        resp = safe_json({
            "status": "success",
            "user": {
                "id": user["id"],
                "username": user["username"],
                "avatar": user.get("google_picture") or user.get("avatar") or "",
            }
        })
        _set_auth_cookies(resp, token)
        return resp
    except Exception as ex:
        logger.error(f"login: {ex}")
        return safe_json({"status": "error", "message": str(ex)}, 500)


@router.post("/api/auth/logout")
async def logout(request: Request, current_user: dict = Depends(get_current_user)):
    revoke_token(current_user["jti"])
    resp = safe_json({"status": "success"})
    _clear_auth_cookies(resp)
    return resp


@router.post("/api/auth/change-password")
async def change_password(body: ChangePasswordIn, current_user: dict = Depends(get_current_user)):
    try:
        uid = current_user["id"]
        with get_db() as (conn, cursor):
            cursor.execute("SELECT password_hash FROM users WHERE id=%s", (uid,))
            row = cursor.fetchone()
            if not row or not row.get("password_hash"):
                return safe_json({"status": "error", "message": "此帳號不支援密碼變更"}, 400)
            if not verify_password(body.current_password, row["password_hash"]):
                return safe_json({"status": "error", "message": "目前密碼錯誤"}, 401)
            cursor.execute("UPDATE users SET password_hash=%s WHERE id=%s", (hash_password(body.new_password), uid))
            # 踢下其他所有裝置（保留當前 session，其餘全部撤銷）
            current_jti = current_user.get("jti", "")
            cursor.execute(
                "UPDATE user_sessions SET is_revoked=1 WHERE user_id=%s AND jti != %s",
                (uid, current_jti)
            )
            conn.commit()
        return safe_json({"status": "success", "message": "密碼已更新，其他裝置已自動登出"})
    except Exception as ex:
        return safe_json({"status": "error", "message": str(ex)}, 500)
