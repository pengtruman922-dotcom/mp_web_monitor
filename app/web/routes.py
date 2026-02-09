"""Web page routes (server-side rendered with Jinja2)."""
from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.source import MonitorSource
from app.models.task import CrawlTask
from app.models.result import CrawlResult
from app.models.report import Report
from app.models.push_rule import PushRule
from app.models.settings import LLMConfig, EmailConfig
from app.scheduler.scheduler import get_scheduler_jobs
from app.agent.prompts import DEFAULT_CRAWL_RULES

templates = Jinja2Templates(directory="app/web/templates")

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    # Sources with their latest task info
    sources_q = await db.execute(select(MonitorSource).order_by(MonitorSource.id))
    sources = list(sources_q.scalars().all())

    source_data = []
    for s in sources:
        # Get latest task for this source
        task_q = await db.execute(
            select(CrawlTask)
            .where(CrawlTask.source_id == s.id)
            .order_by(CrawlTask.created_at.desc())
            .limit(1)
        )
        latest_task = task_q.scalar_one_or_none()
        source_data.append({
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "is_active": s.is_active,
            "last_crawl": str(latest_task.completed_at)[:16] if latest_task and latest_task.completed_at else None,
            "last_items": latest_task.items_found if latest_task else None,
        })

    # Stats
    total_sources = len(sources)
    active_sources = sum(1 for s in sources if s.is_active)
    total_results_q = await db.execute(select(func.count(CrawlResult.id)))
    total_results = total_results_q.scalar() or 0
    total_reports_q = await db.execute(select(func.count(Report.id)))
    total_reports = total_reports_q.scalar() or 0

    last_task_q = await db.execute(
        select(CrawlTask).order_by(CrawlTask.completed_at.desc()).limit(1)
    )
    last_task = last_task_q.scalar_one_or_none()
    last_crawl = str(last_task.completed_at)[:16] if last_task and last_task.completed_at else None

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "active_page": "dashboard",
        "sources": source_data,
        "stats": {
            "total_sources": total_sources,
            "active_sources": active_sources,
            "total_results": total_results,
            "total_reports": total_reports,
            "last_crawl": last_crawl,
        },
    })


@router.get("/sources")
async def sources_page(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(MonitorSource).order_by(MonitorSource.id))
    sources = [
        {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "description": s.description,
            "focus_areas": s.focus_areas or [],
            "max_depth": s.max_depth,
            "content_types": s.content_types or [],
            "schedule": s.schedule,
            "time_range_days": s.time_range_days or 7,
            "max_items": s.max_items or 30,
            "crawl_rules": s.crawl_rules,
            "is_active": s.is_active,
        }
        for s in result.scalars().all()
    ]
    return templates.TemplateResponse("sources.html", {
        "request": request,
        "active_page": "sources",
        "sources": sources,
        "default_crawl_rules": DEFAULT_CRAWL_RULES,
    })


@router.get("/reports")
async def reports_page(request: Request):
    return templates.TemplateResponse("reports.html", {
        "request": request,
        "active_page": "reports",
    })


@router.get("/reports/{report_id}")
async def report_detail_page(report_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    report = await db.get(Report, report_id)
    if not report:
        return templates.TemplateResponse("reports.html", {
            "request": request, "active_page": "reports", "reports": [],
        })
    return templates.TemplateResponse("report_detail.html", {
        "request": request,
        "active_page": "reports",
        "report": {
            "id": report.id,
            "title": report.title,
            "content_html": report.content_html,
            "overview": report.overview,
            "generated_at": str(report.generated_at)[:16] if report.generated_at else None,
        },
    })


@router.get("/tasks")
async def tasks_page(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(CrawlTask).order_by(CrawlTask.created_at.desc()).limit(100)
    )
    tasks = [
        {
            "id": t.id,
            "batch_id": t.batch_id,
            "source_name": t.source_name,
            "status": t.status,
            "triggered_by": t.triggered_by,
            "items_found": t.items_found,
            "error_log": t.error_log,
            "progress_log": t.progress_log or "",
            "started_at": str(t.started_at)[:16] if t.started_at else None,
            "completed_at": str(t.completed_at)[:16] if t.completed_at else None,
        }
        for t in result.scalars().all()
    ]
    return templates.TemplateResponse("tasks.html", {
        "request": request,
        "active_page": "tasks",
        "tasks": tasks,
    })


@router.get("/push-rules")
async def push_rules_page(request: Request, db: AsyncSession = Depends(get_db)):
    rules_q = await db.execute(select(PushRule).order_by(PushRule.id))
    sources_q = await db.execute(select(MonitorSource))
    sources_map = {s.id: s.name for s in sources_q.scalars().all()}

    rules = []
    for r in rules_q.scalars().all():
        source_names = [sources_map.get(sid, f"ID:{sid}") for sid in (r.source_ids or [])]
        rules.append({
            "id": r.id,
            "name": r.name,
            "source_ids": r.source_ids or [],
            "source_names": source_names,
            "channel": r.channel,
            "recipients": r.recipients or [],
            "push_mode": r.push_mode or "on_update",
            "push_schedule": r.push_schedule or "",
            "is_active": r.is_active,
        })

    return templates.TemplateResponse("push_rules.html", {
        "request": request,
        "active_page": "push_rules",
        "rules": rules,
    })


@router.get("/settings")
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    llm_q = await db.execute(select(LLMConfig).where(LLMConfig.is_active == True).limit(1))
    llm = llm_q.scalar_one_or_none()

    email_q = await db.execute(select(EmailConfig).limit(1))
    email = email_q.scalar_one_or_none()

    llm_data = None
    if llm:
        llm_data = {
            "name": llm.name,
            "api_url": llm.api_url,
            "api_key": llm.api_key[:3] + "***" + llm.api_key[-4:] if len(llm.api_key) > 8 else "***",
            "model_name": llm.model_name,
        }

    email_data = None
    if email:
        email_data = {
            "smtp_host": email.smtp_host,
            "smtp_port": email.smtp_port,
            "use_tls": email.use_tls,
            "username": email.username,
            "password": "***" if email.password else "",
            "sender_email": email.sender_email,
            "sender_name": email.sender_name,
        }

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "active_page": "settings",
        "llm": llm_data,
        "email": email_data,
        "scheduler_jobs": get_scheduler_jobs(),
    })
