"""Authentication API: login, logout, current user, change password."""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.user import User
from app.auth import (
    verify_password, hash_password,
    set_session_cookie, clear_session_cookie,
    get_current_user,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.post("/login")
async def login(data: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(User).where(User.username == data.username)
    )
    user = result.scalar_one_or_none()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(400, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(403, "账号已停用")
    set_session_cookie(response, user.id)
    return {
        "ok": True,
        "user": {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
        },
        "must_change_password": user.must_change_password,
    }


@router.post("/logout")
async def logout(response: Response):
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "must_change_password": user.must_change_password,
    }


@router.put("/change-password")
async def change_password(
    data: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not verify_password(data.old_password, user.password_hash):
        raise HTTPException(400, "原密码错误")
    if len(data.new_password) < 6:
        raise HTTPException(400, "新密码至少6位")
    user.password_hash = hash_password(data.new_password)
    user.must_change_password = False
    await db.commit()
    return {"ok": True}
