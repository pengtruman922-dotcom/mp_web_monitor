"""User management API (admin only)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.user import User
from app.auth import require_admin, hash_password

router = APIRouter(prefix="/api/users", tags=["users"])


class UserCreate(BaseModel):
    username: str
    display_name: str = ""
    password: str
    role: str = "user"


class UserUpdate(BaseModel):
    display_name: str | None = None
    role: str | None = None
    is_active: bool | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str


@router.get("")
async def list_users(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = await db.execute(select(User).order_by(User.id))
    users = result.scalars().all()
    return [_to_dict(u) for u in users]


@router.post("")
async def create_user(
    data: UserCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    # Check duplicate username
    existing = await db.execute(select(User).where(User.username == data.username))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "用户名已存在")
    if len(data.password) < 6:
        raise HTTPException(400, "密码至少6位")
    if data.role not in ("admin", "user"):
        raise HTTPException(400, "角色必须是 admin 或 user")
    user = User(
        username=data.username,
        display_name=data.display_name or data.username,
        password_hash=hash_password(data.password),
        role=data.role,
        is_active=True,
        must_change_password=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return _to_dict(user)


@router.put("/{user_id}")
async def update_user(
    user_id: int,
    data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")

    # Prevent removing the last admin
    if data.role and data.role != "admin" and user.role == "admin":
        count_q = await db.execute(
            select(func.count(User.id)).where(User.role == "admin", User.is_active == True)
        )
        if (count_q.scalar() or 0) <= 1:
            raise HTTPException(400, "不能取消最后一个管理员的管理权限")

    if data.is_active is False and user.role == "admin":
        count_q = await db.execute(
            select(func.count(User.id)).where(User.role == "admin", User.is_active == True)
        )
        if (count_q.scalar() or 0) <= 1:
            raise HTTPException(400, "不能停用最后一个管理员")

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(user, key, value)
    await db.commit()
    await db.refresh(user)
    return _to_dict(user)


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: int,
    data: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    if len(data.new_password) < 6:
        raise HTTPException(400, "密码至少6位")
    user.password_hash = hash_password(data.new_password)
    user.must_change_password = True
    await db.commit()
    return {"ok": True}


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    if user.id == admin.id:
        raise HTTPException(400, "不能删除自己")
    if user.role == "admin":
        count_q = await db.execute(
            select(func.count(User.id)).where(User.role == "admin", User.is_active == True)
        )
        if (count_q.scalar() or 0) <= 1:
            raise HTTPException(400, "不能删除最后一个管理员")
    # Soft delete: deactivate
    user.is_active = False
    await db.commit()
    return {"ok": True}


def _to_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "display_name": u.display_name,
        "role": u.role,
        "is_active": u.is_active,
        "must_change_password": u.must_change_password,
        "created_at": str(u.created_at) if u.created_at else None,
    }
