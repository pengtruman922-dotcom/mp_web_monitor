"""CrawlResult query / delete API."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.result import CrawlResult
from app.models.task import CrawlTask
from app.models.user import User
from app.auth import get_current_user, get_effective_user_id

router = APIRouter(prefix="/api/results", tags=["results"])


class BatchDeleteRequest(BaseModel):
    ids: list[int]


@router.get("")
async def list_results(
    source_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    tag: str | None = None,
    sort: str = "desc",
    limit: int = 100,
    view_user_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Query crawl results with optional filtering and sorting."""
    uid = get_effective_user_id(user, view_user_id)

    # Build a query that joins CrawlTask to get source_name
    stmt = select(
        CrawlResult,
        CrawlTask.source_name,
    ).join(CrawlTask, CrawlResult.task_id == CrawlTask.id)

    if uid is not None:
        stmt = stmt.where(CrawlResult.user_id == uid)

    if source_id is not None:
        stmt = stmt.where(CrawlResult.source_id == source_id)

    if tag:
        stmt = stmt.where(CrawlResult.tags.contains(tag))

    if date_from or date_to:
        stmt = stmt.where(CrawlResult.published_date.isnot(None))

    if date_from:
        try:
            d = datetime.strptime(date_from, "%Y-%m-%d").date()
            stmt = stmt.where(CrawlResult.published_date >= d)
        except ValueError:
            raise HTTPException(400, "date_from 格式应为 YYYY-MM-DD")

    if date_to:
        try:
            d = datetime.strptime(date_to, "%Y-%m-%d").date()
            stmt = stmt.where(CrawlResult.published_date <= d)
        except ValueError:
            raise HTTPException(400, "date_to 格式应为 YYYY-MM-DD")

    if sort == "asc":
        stmt = stmt.order_by(CrawlResult.published_date.asc())
    else:
        stmt = stmt.order_by(CrawlResult.published_date.desc())

    stmt = stmt.limit(limit)

    rows = await db.execute(stmt)
    results = rows.all()

    return [
        {
            "id": r.CrawlResult.id,
            "source_id": r.CrawlResult.source_id,
            "source_name": r.source_name or "",
            "title": r.CrawlResult.title,
            "url": r.CrawlResult.url,
            "content_type": r.CrawlResult.content_type,
            "summary": r.CrawlResult.summary,
            "tags": [t for t in (r.CrawlResult.tags or "").split(",") if t],
            "published_date": str(r.CrawlResult.published_date) if r.CrawlResult.published_date else None,
            "crawled_at": str(r.CrawlResult.crawled_at)[:16] if r.CrawlResult.crawled_at else None,
            "has_attachment": r.CrawlResult.has_attachment,
            "attachment_name": r.CrawlResult.attachment_name or "",
        }
        for r in results
    ]


@router.get("/tags")
async def list_tags(
    view_user_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return all unique tags with counts."""
    uid = get_effective_user_id(user, view_user_id)
    stmt = select(CrawlResult.tags).where(CrawlResult.tags != "")
    if uid is not None:
        stmt = stmt.where(CrawlResult.user_id == uid)
    rows = await db.execute(stmt)
    tag_counts: dict[str, int] = {}
    for (tags_str,) in rows:
        for tag in tags_str.split(","):
            tag = tag.strip()
            if tag:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
    # Sort by count descending
    sorted_tags = sorted(tag_counts.items(), key=lambda x: -x[1])
    return [{"tag": t, "count": c} for t, c in sorted_tags]


# --- Static path routes MUST come before /{result_id} ---

@router.post("/batch-delete")
async def batch_delete_results(body: BatchDeleteRequest, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    if not body.ids:
        return {"ok": True, "deleted": 0}
    stmt = delete(CrawlResult).where(CrawlResult.id.in_(body.ids))
    if user.role != "admin":
        stmt = stmt.where(CrawlResult.user_id == user.id)
    result = await db.execute(stmt)
    await db.commit()
    return {"ok": True, "deleted": result.rowcount}


@router.delete("/all")
async def delete_all_results(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    stmt = delete(CrawlResult)
    if user.role != "admin":
        stmt = stmt.where(CrawlResult.user_id == user.id)
    result = await db.execute(stmt)
    await db.commit()
    return {"ok": True, "deleted": result.rowcount}


# --- Dynamic path route ---

@router.delete("/{result_id}")
async def delete_result(result_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    obj = await db.get(CrawlResult, result_id)
    if not obj:
        raise HTTPException(404, "结果不存在")
    if user.role != "admin" and obj.user_id != user.id:
        raise HTTPException(403, "无权限")
    await db.delete(obj)
    await db.commit()
    return {"ok": True}
