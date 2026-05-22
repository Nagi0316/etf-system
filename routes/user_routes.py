"""
routes/user_routes.py — 使用者個人資料、大頭照上傳
"""
import os, time, logging
from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.templating import Jinja2Templates

from auth import get_current_user
from models import UpdateProfileIn
from database import get_db
from utils import safe_json
from config import AVATAR_DIR

logger = logging.getLogger(__name__)
router = APIRouter()
templates: Jinja2Templates | None = None


@router.get("/api/user/profile")
async def get_profile(current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    with get_db() as (conn, cursor):
        cursor.execute(
            "SELECT id, username, email, phone, avatar, google_picture, monthly_budget, created_at FROM users WHERE id=%s",
            (uid,)
        )
        user = cursor.fetchone()
    if not user:
        return safe_json({"status": "error", "message": "用戶不存在"}, 404)
    if hasattr(user.get("created_at"), "strftime"):
        user["created_at"] = user["created_at"].strftime("%Y-%m-%d")
    user["display_avatar"] = user.get("google_picture") or user.get("avatar") or ""
    return safe_json({"status": "success", "data": user})


@router.put("/api/user/profile")
async def update_profile(body: UpdateProfileIn, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    with get_db() as (conn, cursor):
        fields, vals = [], []
        if body.username is not None:
            fields.append("username=%s"); vals.append(body.username)
        if body.phone is not None:
            fields.append("phone=%s"); vals.append(body.phone)
        if body.monthly_budget is not None:
            fields.append("monthly_budget=%s"); vals.append(body.monthly_budget)
        if not fields:
            return safe_json({"status": "error", "message": "沒有要更新的欄位"}, 400)
        vals.append(uid)
        cursor.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=%s", vals)
        conn.commit()
    return safe_json({"status": "success", "message": "更新成功"})


@router.post("/api/user/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    uid = current_user["id"]
    if not file.content_type.startswith("image/"):
        return safe_json({"status": "error", "message": "請上傳圖片檔案"}, 400)
    if file.size and file.size > 5 * 1024 * 1024:
        return safe_json({"status": "error", "message": "圖片大小不能超過 5MB"}, 400)

    ext = (file.filename or "jpg").rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
        ext = "jpg"
    fname = f"avatar_{uid}_{int(time.time())}.{ext}"
    fpath = os.path.join(AVATAR_DIR, fname)

    content = await file.read()
    with open(fpath, "wb") as fp:
        fp.write(content)

    avatar_url = f"/static/uploads/avatars/{fname}"
    with get_db() as (conn, cursor):
        cursor.execute("UPDATE users SET avatar=%s WHERE id=%s", (avatar_url, uid))
        conn.commit()
    return safe_json({"status": "success", "avatar": avatar_url})
