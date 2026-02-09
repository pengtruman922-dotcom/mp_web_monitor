"""Report viewing API."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.report import Report
from app.models.result import CrawlResult
from app.models.task import CrawlTask

router = APIRouter(prefix="/api/reports", tags=["reports"])


class BatchDeleteRequest(BaseModel):
    ids: list[int]


@router.get("")
async def list_reports(
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "desc",
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Report)

    if date_from:
        try:
            dt = datetime.strptime(date_from, "%Y-%m-%d")
            stmt = stmt.where(Report.generated_at >= dt)
        except ValueError:
            raise HTTPException(400, "date_from 格式应为 YYYY-MM-DD")

    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            stmt = stmt.where(Report.generated_at <= dt)
        except ValueError:
            raise HTTPException(400, "date_to 格式应为 YYYY-MM-DD")

    if sort == "asc":
        stmt = stmt.order_by(Report.generated_at.asc())
    else:
        stmt = stmt.order_by(Report.generated_at.desc())

    stmt = stmt.limit(limit)

    result = await db.execute(stmt)
    reports = result.scalars().all()
    return [
        {
            "id": r.id,
            "batch_id": r.batch_id,
            "title": r.title,
            "generated_at": str(r.generated_at)[:16] if r.generated_at else None,
        }
        for r in reports
    ]


# --- Static path routes MUST come before /{report_id} ---

@router.post("/batch-delete")
async def batch_delete_reports(body: BatchDeleteRequest, db: AsyncSession = Depends(get_db)):
    if not body.ids:
        return {"ok": True, "deleted": 0}
    result = await db.execute(
        delete(Report).where(Report.id.in_(body.ids))
    )
    await db.commit()
    return {"ok": True, "deleted": result.rowcount}


@router.delete("/all")
async def delete_all_reports(db: AsyncSession = Depends(get_db)):
    result = await db.execute(delete(Report))
    await db.commit()
    return {"ok": True, "deleted": result.rowcount}


# --- Dynamic path routes ---

@router.get("/{report_id}")
async def get_report(report_id: int, db: AsyncSession = Depends(get_db)):
    report = await db.get(Report, report_id)
    if not report:
        raise HTTPException(404, "报告不存在")
    return {
        "id": report.id,
        "batch_id": report.batch_id,
        "title": report.title,
        "content_html": report.content_html,
        "content_text": report.content_text,
        "overview": report.overview,
        "generated_at": str(report.generated_at) if report.generated_at else None,
    }


@router.delete("/{report_id}")
async def delete_report(report_id: int, db: AsyncSession = Depends(get_db)):
    obj = await db.get(Report, report_id)
    if not obj:
        raise HTTPException(404, "报告不存在")
    await db.delete(obj)
    await db.commit()
    return {"ok": True}


@router.get("/{report_id}/results")
async def get_report_results(report_id: int, db: AsyncSession = Depends(get_db)):
    """Get the detailed crawl results for a report."""
    report = await db.get(Report, report_id)
    if not report:
        raise HTTPException(404, "报告不存在")

    # Find tasks for this batch
    tasks_q = await db.execute(
        select(CrawlTask).where(CrawlTask.batch_id == report.batch_id)
    )
    tasks = list(tasks_q.scalars().all())
    task_ids = [t.id for t in tasks]

    # Fetch results
    results_q = await db.execute(
        select(CrawlResult).where(CrawlResult.task_id.in_(task_ids))
        .order_by(CrawlResult.source_id, CrawlResult.published_date.desc())
    )
    results = results_q.scalars().all()

    return [
        {
            "id": r.id,
            "source_id": r.source_id,
            "title": r.title,
            "url": r.url,
            "content_type": r.content_type,
            "summary": r.summary,
            "has_attachment": r.has_attachment,
            "attachment_name": r.attachment_name,
            "attachment_summary": r.attachment_summary,
            "published_date": str(r.published_date) if r.published_date else None,
            "crawled_at": str(r.crawled_at) if r.crawled_at else None,
        }
        for r in results
    ]
