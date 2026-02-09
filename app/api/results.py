"""CrawlResult query / delete API."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.result import CrawlResult
from app.models.task import CrawlTask

router = APIRouter(prefix="/api/results", tags=["results"])


class BatchDeleteRequest(BaseModel):
    ids: list[int]


@router.get("")
async def list_results(
    source_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "desc",
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    """Query crawl results with optional filtering and sorting."""
    # Build a query that joins CrawlTask to get source_name
    stmt = select(
        CrawlResult,
        CrawlTask.source_name,
    ).join(CrawlTask, CrawlResult.task_id == CrawlTask.id)

    if source_id is not None:
        stmt = stmt.where(CrawlResult.source_id == source_id)

    if date_from:
        try:
            dt = datetime.strptime(date_from, "%Y-%m-%d")
            stmt = stmt.where(CrawlResult.crawled_at >= dt)
        except ValueError:
            raise HTTPException(400, "date_from 格式应为 YYYY-MM-DD")

    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d")
            # Include the whole day
            dt = dt.replace(hour=23, minute=59, second=59)
            stmt = stmt.where(CrawlResult.crawled_at <= dt)
        except ValueError:
            raise HTTPException(400, "date_to 格式应为 YYYY-MM-DD")

    if sort == "asc":
        stmt = stmt.order_by(CrawlResult.crawled_at.asc())
    else:
        stmt = stmt.order_by(CrawlResult.crawled_at.desc())

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
            "published_date": str(r.CrawlResult.published_date) if r.CrawlResult.published_date else None,
            "crawled_at": str(r.CrawlResult.crawled_at)[:16] if r.CrawlResult.crawled_at else None,
            "has_attachment": r.CrawlResult.has_attachment,
            "attachment_name": r.CrawlResult.attachment_name or "",
        }
        for r in results
    ]


# --- Static path routes MUST come before /{result_id} ---

@router.post("/batch-delete")
async def batch_delete_results(body: BatchDeleteRequest, db: AsyncSession = Depends(get_db)):
    if not body.ids:
        return {"ok": True, "deleted": 0}
    result = await db.execute(
        delete(CrawlResult).where(CrawlResult.id.in_(body.ids))
    )
    await db.commit()
    return {"ok": True, "deleted": result.rowcount}


@router.delete("/all")
async def delete_all_results(db: AsyncSession = Depends(get_db)):
    result = await db.execute(delete(CrawlResult))
    await db.commit()
    return {"ok": True, "deleted": result.rowcount}


# --- Dynamic path route ---

@router.delete("/{result_id}")
async def delete_result(result_id: int, db: AsyncSession = Depends(get_db)):
    obj = await db.get(CrawlResult, result_id)
    if not obj:
        raise HTTPException(404, "结果不存在")
    await db.delete(obj)
    await db.commit()
    return {"ok": True}
