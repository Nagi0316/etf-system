"""
auth.py — JWT 驗證、Google OAuth 2.0、bcrypt 密碼處理
"""
import uuid, logging, httpx
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext

from config import (
    JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRE_HOURS,
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
)
from database import get_db

logger = logging.getLogger(__name__)

# ── bcrypt ──
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(plain: str) -> str:
    return pwd_ctx.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    # 向下相容舊 SHA-256（如果 hash 是 64-char hex，改用 sha256 比對）
    if len(hashed) == 64 and all(c in "0123456789abcdef" for c in hashed):
        import hashlib
        return hashlib.sha256(plain.encode()).hexdigest() == hashed
    return pwd_ctx.verify(plain, hashed)


# ══════════════════════════════════════════════════════════
#  JWT
# ══════════════════════════════════════════════════════════

def create_access_token(user_id: int, email: str) -> tuple[str, str]:
    """回傳 (token, jti)"""
    jti = uuid.uuid4().hex
    expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {
        "sub": str(user_id),
        "email": email,
        "jti": jti,
        "exp": expire,
        "iat": datetime.utcnow(),
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
    # 檢查是否被撤銷
    jti = payload.get("jti")
    if jti:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT is_revoked FROM user_sessions WHERE jti=%s", (jti,))
            row = cursor.fetchone()
        if row and row["is_revoked"]:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 已失效，請重新登入")
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

_oauth_states: dict[str, str] = {}  # state -> redirect_after


def build_google_login_url(redirect_after: str = "/") -> str:
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google OAuth 未設定")
    state = uuid.uuid4().hex
    _oauth_states[state] = redirect_after
    params = (
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={GOOGLE_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=openid%20email%20profile"
        f"&state={state}"
        f"&access_type=offline"
        f"&prompt=select_account"
    )
    return GOOGLE_AUTH_URL + params


async def exchange_google_code(code: str, state: str) -> dict:
    """用 code 換取 Google user info"""
    _ = _oauth_states.pop(state, None)  # 防止 CSRF，用完即丟
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
        return info_resp.json()
