"""
auth.py — JWT 驗證、Google OAuth 2.0、bcrypt 密碼處理
"""
import hashlib, hmac, uuid, logging, httpx
import bcrypt
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from config import (
    JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRE_HOURS,
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
)
from database import get_db

logger = logging.getLogger(__name__)

# ── bcrypt ──
# passlib 1.7.4 與 bcrypt 5.x 不相容，會讓正常密碼也拋出 72-byte 錯誤。
# 新密碼先做 SHA-256 再 bcrypt，可支援模型允許的 128 字元，並以標記區分；
# 舊版標準 bcrypt 與更早期 SHA-256 雜湊仍可驗證。
_BCRYPT_SHA256_PREFIX = "bcrypt_sha256$"

def hash_password(plain: str) -> str:
    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    hashed = bcrypt.hashpw(digest, bcrypt.gensalt(rounds=12)).decode("ascii")
    return _BCRYPT_SHA256_PREFIX + hashed

def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    if hashed.startswith(_BCRYPT_SHA256_PREFIX):
        digest = hashlib.sha256(plain.encode("utf-8")).digest()
        try:
            return bcrypt.checkpw(
                digest,
                hashed[len(_BCRYPT_SHA256_PREFIX):].encode("ascii"),
            )
        except ValueError:
            return False
    # 向下相容舊 SHA-256（如果 hash 是 64-char hex，改用 sha256 比對）
    if len(hashed) == 64 and all(c in "0123456789abcdef" for c in hashed):
        candidate = hashlib.sha256(plain.encode()).hexdigest()
        return hmac.compare_digest(candidate, hashed)
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, UnicodeError):
        return False


# ══════════════════════════════════════════════════════════
#  JWT
# ══════════════════════════════════════════════════════════

def create_access_token(user_id: int, email: str) -> tuple[str, str]:
    """回傳 (token, jti)"""
    jti = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    expire = now + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {
        "sub": str(user_id),
        "email": email,
        "jti": jti,
        "exp": expire,
        "iat": now,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    # 儲存 jti 供撤銷用
    with get_db() as (conn, cursor):
        cursor.execute(
            "INSERT INTO user_sessions (user_id, jti, expires_at) VALUES (%s,%s,%s)",
            (user_id, jti, expire.strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
    return token, jti


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None


def revoke_token(jti: str):
    with get_db() as (conn, cursor):
        cursor.execute("UPDATE user_sessions SET is_revoked=1 WHERE jti=%s", (jti,))
        conn.commit()
    # 立即清除快取，確保登出後不會再有 cache-hit 通過驗證
    try:
        from cache import cache
        cache.delete(f"jti:ok:{jti}")
    except Exception:
        pass


# ── Bearer 依賴注入 ──
_bearer = HTTPBearer(auto_error=False)

def _get_token_from_request(request: Request, credentials: Optional[HTTPAuthorizationCredentials]) -> Optional[str]:
    # 1. Authorization: Bearer <token>
    if credentials and credentials.scheme.lower() == "bearer":
        return credentials.credentials
    # 2. Cookie (前端頁面友善)
    return request.cookies.get("access_token")


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    token = _get_token_from_request(request, credentials)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登入")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 無效或已過期")
    # 檢查是否被撤銷（JTI 驗證加快取，避免每次 API 都打 DB）
    jti = payload.get("jti")
    if jti:
        from cache import cache
        cache_key         = f"jti:ok:{jti}"
        revoked_cache_key = f"jti:revoked:{jti}"
        # 快速路徑：已知撤銷（負快取 60s），避免每次打 DB
        if cache.get(revoked_cache_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 已失效，請重新登入")
        if not cache.get(cache_key):
            # Cache miss → 查 DB
            try:
                with get_db() as (conn, cursor):
                    cursor.execute("SELECT is_revoked FROM user_sessions WHERE jti=%s", (jti,))
                    row = cursor.fetchone()
            except Exception as _db_err:
                # DB 不可用時拒絕請求（fail-closed），避免已撤銷 token 通過驗證
                logger.warning("JTI DB 驗證失敗，拒絕請求以確保安全: %s", _db_err)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="認證服務暫時不可用，請稍後再試",
                )
            if row and row["is_revoked"]:
                cache.set(revoked_cache_key, 1, 60)  # 負快取 60s，減少 DB 查詢
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 已失效，請重新登入")
            if row:
                # 只有確認有效才快取（5 分鐘），DB 無此紀錄（row is None）時不快取
                cache.set(cache_key, 1, 300)
    return {"id": int(payload["sub"]), "email": payload.get("email", ""), "jti": jti}


def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[dict]:
    try:
        return get_current_user(request, credentials)
    except HTTPException:
        return None


# ══════════════════════════════════════════════════════════
#  Google OAuth 2.0
# ══════════════════════════════════════════════════════════

GOOGLE_AUTH_URL    = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL   = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

_OAUTH_STATE_TTL = 300  # OAuth state 5 分鐘後過期，防止 CSRF 攻擊窗口無限開放


def _store_oauth_state(state: str, redirect_after: str):
    """將 OAuth state 存入有 TTL 的 cache，防止記憶體無限增長。"""
    from cache import cache
    cache.set(f"oauth_state:{state}", redirect_after, _OAUTH_STATE_TTL)


def _consume_oauth_state(state: str) -> Optional[str]:
    """取出並刪除 OAuth state（一次性使用），防止 replay 攻擊。"""
    from cache import cache
    key = f"oauth_state:{state}"
    val = cache.get(key)
    if val is not None:
        cache.delete(key)
    return val


def safe_redirect_path(value: str = "/") -> str:
    """OAuth 完成後只允許站內絕對路徑，避免 //host 類型的開放重新導向。"""
    value = (value or "/").strip()
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    if value.split("?", 1)[0] in {"/auth", "/login", "/api/auth/google/login",
                                  "/api/auth/google/callback"}:
        return "/"
    return value


def build_google_login_url(redirect_after: str = "/") -> str:
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google OAuth 未設定")
    state = uuid.uuid4().hex
    _store_oauth_state(state, safe_redirect_path(redirect_after))
    params = urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    })
    return f"{GOOGLE_AUTH_URL}?{params}"


async def exchange_google_code(code: str, state: str) -> tuple[dict, str]:
    """用 code 換取 Google user info，並回傳 state 綁定的站內目的頁。"""
    expected_redirect = _consume_oauth_state(state)
    if not expected_redirect:
        raise HTTPException(status_code=400, detail="無效或過期的 OAuth State，請重新登入")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        })
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Google OAuth token 交換失敗")
        tokens = resp.json()

        info_resp = await client.get(GOOGLE_USERINFO_URL, headers={
            "Authorization": f"Bearer {tokens['access_token']}"
        })
        if info_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="無法取得 Google 使用者資訊")
        return info_resp.json(), safe_redirect_path(expected_redirect)
