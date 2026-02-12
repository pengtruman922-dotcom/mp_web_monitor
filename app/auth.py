"""Authentication utilities: password hashing, session tokens, FastAPI dependencies."""
import hashlib
import logging
import os
from typing import Optional

from fastapi import Depends, HTTPException, Request, Response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import SECRET_KEY
from app.database.connection import get_db
from app.models.user import User

logger = logging.getLogger(__name__)

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="session")

SESSION_COOKIE = "session_token"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days


def hash_password(plain: str) -> str:
    """Hash a password using PBKDF2-SHA256 with a random salt."""
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt.encode(), 260000)
    return f"pbkdf2:sha256:260000${salt}${dk.hex()}"


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password against its PBKDF2 hash."""
    try:
        # Format: "pbkdf2:sha256:260000$<salt>$<dk_hex>"
        prefix = "pbkdf2:sha256:260000$"
        if not hashed.startswith(prefix):
            return False
        rest = hashed[len(prefix):]
        salt, stored_dk = rest.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt.encode(), 260000)
        return dk.hex() == stored_dk
    except Exception:
        return False


def create_session_token(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def decode_session_token(token: str) -> Optional[int]:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return data.get("uid")
    except (BadSignature, SignatureExpired):
        return None


def set_session_cookie(response: Response, user_id: int):
    token = create_session_token(user_id)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response):
    response.delete_cookie(SESSION_COOKIE)


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI dependency: returns the logged-in User or raises 401."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    user_id = decode_session_token(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="会话已过期，请重新登录")
    user = await db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="账号不存在或已停用")
    return user


async def require_admin(
    user: User = Depends(get_current_user),
) -> User:
    """FastAPI dependency: requires the current user to be an admin."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def get_effective_user_id(user: User, view_user_id: Optional[int] = None) -> Optional[int]:
    """Determine which user_id to filter by.

    - Normal users always see their own data (returns user.id).
    - Admins: view_user_id=None or view_user_id=user.id -> own data
              view_user_id=0 -> all data (returns None)
              view_user_id=N -> that user's data
    """
    if user.role != "admin":
        return user.id
    if view_user_id is None:
        return user.id
    if view_user_id == 0:
        return None  # all users
    return view_user_id
