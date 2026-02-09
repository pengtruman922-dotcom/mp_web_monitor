"""Task management API: trigger crawls, view task status."""
import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.task import CrawlTask, TaskStatus
from app.models.result import CrawlResult
from app.agent.orchestrator import run_batch, is_running, get_running_sources, request_cancel, release_source

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TriggerRequest(BaseModel):
    source_ids: list[int] | None = None  # None = all active sources


async def _run_batch_safe(source_ids: list[int] | None):
    """Wrapper that catches and logs exceptions from background batch runs."""
    try:
        batch_id = await run_batch(source_ids=source_ids)
        logger.info("Background batch %s completed", batch_id)
    except Exception:
        logger.exception("Background batch failed")


@router.post("/trigger")
async def trigger_crawl(data: TriggerRequest):
    """Manually trigger a crawl batch."""
    running = get_running_sources()

    if data.source_ids:
        # Check if the specific requested sources are already running
        already = [sid for sid in data.source_ids if sid in running]
        if already and len(already) == len(data.source_ids):
            raise HTTPException(409, "所有请求的监控源都正在采集中，请等待完成")
    else:
        # "All sources" mode — only block if every source is already running
        # (let run_batch filter out duplicates)
        pass

    # Run in background so the API returns immediately
    task = asyncio.create_task(_run_batch_safe(data.source_ids))
    # Prevent the task from being garbage-collected before completion
    task.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
    return {"message": "采集任务已启动", "status": "started"}


@router.get("")
async def list_tasks(limit: int = 50, db: AsyncSession = Depends(get_db)):
    """List recent crawl tasks."""
    result = await db.execute(
        select(CrawlTask).order_by(CrawlTask.created_at.desc()).limit(limit)
    )
    tasks = result.scalars().all()
    return [_to_dict(t) for t in tasks]


@router.get("/batch/{batch_id}")
async def get_batch(batch_id: str, db: AsyncSession = Depends(get_db)):
    """Get all tasks for a batch."""
    result = await db.execute(
        select(CrawlTask).where(CrawlTask.batch_id == batch_id).order_by(CrawlTask.source_id)
    )
    tasks = result.scalars().all()
    return [_to_dict(t) for t in tasks]


@router.get("/running")
async def check_running(db: AsyncSession = Depends(get_db)):
    """Check if a crawl batch is currently running, and which sources."""
    memory_running = set(get_running_sources())

    # Also check DB for tasks with status "running" (handles server restart, race conditions)
    result = await db.execute(
        select(CrawlTask.source_id)
        .where(CrawlTask.status == TaskStatus.running.value)
        .distinct()
    )
    db_running = {row[0] for row in result.all()}

    all_running = list(memory_running | db_running)
    return {
        "running": bool(all_running),
        "running_source_ids": all_running,
    }


@router.get("/{task_id}/progress")
async def get_task_progress(task_id: int, db: AsyncSession = Depends(get_db)):
    """Get real-time progress for a running task."""
    task = await db.get(CrawlTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return {
        "id": task.id,
        "status": task.status,
        "items_found": task.items_found,
        "progress_log": task.progress_log or "",
    }


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """Cancel a running task immediately."""
    task = await db.get(CrawlTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status != TaskStatus.running.value:
        raise HTTPException(400, "只能中止运行中的任务")

    # 1. Signal the agent loop to stop
    request_cancel(task_id)

    # 2. Immediately update task status in DB
    task.status = TaskStatus.cancelled.value
    task.completed_at = datetime.utcnow()
    task.items_found = 0
    task.error_log = "任务被用户中止"

    # 3. Delete any crawl results already produced by this task
    await db.execute(sa_delete(CrawlResult).where(CrawlResult.task_id == task_id))
    await db.commit()

    # 4. Release source so it can be re-triggered
    release_source(task.source_id)

    return {"ok": True, "source_id": task.source_id}


@router.delete("/clear-finished")
async def clear_finished_tasks(db: AsyncSession = Depends(get_db)):
    """Delete all completed and cancelled tasks."""
    result = await db.execute(
        sa_delete(CrawlTask).where(
            CrawlTask.status.in_([TaskStatus.completed.value, TaskStatus.cancelled.value])
        )
    )
    await db.commit()
    return {"ok": True, "deleted": result.rowcount}


def _to_dict(t: CrawlTask) -> dict:
    return {
        "id": t.id,
        "batch_id": t.batch_id,
        "source_id": t.source_id,
        "source_name": t.source_name,
        "status": t.status,
        "triggered_by": t.triggered_by,
        "started_at": str(t.started_at) if t.started_at else None,
        "completed_at": str(t.completed_at) if t.completed_at else None,
        "items_found": t.items_found,
        "error_log": t.error_log,
        "progress_log": t.progress_log or "",
        "created_at": str(t.created_at) if t.created_at else None,
    }
