"""Monitor source CRUD API."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.source import MonitorSource
from app.models.user import User
from app.auth import get_current_user, get_effective_user_id

router = APIRouter(prefix="/api/sources", tags=["sources"])


class SourceCreate(BaseModel):
    name: str
    url: str
    description: str = ""
    focus_areas: list[str] = []
    max_depth: int = 3
    content_types: list[str] = ["news", "policy", "notice", "file"]
    schedule: str = "0 9 * * 1"
    time_range_days: int = 7
    max_items: int = 30
    crawl_rules: str | None = None
    is_active: bool = True


class SourceUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    description: str | None = None
    focus_areas: list[str] | None = None
    max_depth: int | None = None
    content_types: list[str] | None = None
    schedule: str | None = None
    time_range_days: int | None = None
    max_items: int | None = None
    crawl_rules: str | None = None
    is_active: bool | None = None


@router.get("")
async def list_sources(
    view_user_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    uid = get_effective_user_id(user, view_user_id)
    stmt = select(MonitorSource).order_by(MonitorSource.id)
    if uid is not None:
        stmt = stmt.where(MonitorSource.user_id == uid)
    result = await db.execute(stmt)
    sources = result.scalars().all()
    return [_to_dict(s) for s in sources]


@router.get("/{source_id}")
async def get_source(source_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    source = await db.get(MonitorSource, source_id)
    if not source:
        raise HTTPException(404, "监控源不存在")
    if user.role != "admin" and source.user_id != user.id:
        raise HTTPException(403, "无权限")
    return _to_dict(source)


@router.post("")
async def create_source(data: SourceCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    source = MonitorSource(**data.model_dump())
    source.user_id = user.id
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return _to_dict(source)


@router.put("/{source_id}")
async def update_source(source_id: int, data: SourceUpdate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    source = await db.get(MonitorSource, source_id)
    if not source:
        raise HTTPException(404, "监控源不存在")
    if user.role != "admin" and source.user_id != user.id:
        raise HTTPException(403, "无权限")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(source, key, value)
    await db.commit()
    await db.refresh(source)
    return _to_dict(source)


@router.delete("/{source_id}")
async def delete_source(source_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    source = await db.get(MonitorSource, source_id)
    if not source:
        raise HTTPException(404, "监控源不存在")
    if user.role != "admin" and source.user_id != user.id:
        raise HTTPException(403, "无权限")
    await db.delete(source)
    await db.commit()
    return {"ok": True}


def _to_dict(s: MonitorSource) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "url": s.url,
        "description": s.description,
        "focus_areas": s.focus_areas,
        "max_depth": s.max_depth,
        "content_types": s.content_types,
        "schedule": s.schedule,
        "time_range_days": s.time_range_days,
        "max_items": s.max_items,
        "crawl_rules": s.crawl_rules,
        "is_active": s.is_active,
        "created_at": str(s.created_at) if s.created_at else None,
        "updated_at": str(s.updated_at) if s.updated_at else None,
    }
