"""Web page routes (server-side rendered with Jinja2)."""
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.source import MonitorSource
from app.models.task import CrawlTask
from app.models.result import CrawlResult
from app.models.report import Report
from app.models.push_rule import PushRule
from app.models.user import User
from app.models.settings import LLMConfig, EmailConfig
from app.scheduler.scheduler import get_scheduler_jobs
from app.agent.prompts import DEFAULT_CRAWL_RULES
from app.auth import get_current_user, get_effective_user_id, SESSION_COOKIE, decode_session_token

templates = Jinja2Templates(directory="app/web/templates")

router = APIRouter()


async def _try_get_user(request: Request, db: AsyncSession) -> User | None:
    """Try to get the current user without raising - for login page logic."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    user_id = decode_session_token(token)
    if user_id is None:
        return None
    user = await db.get(User, user_id)
    if not user or not user.is_active:
        return None
    return user


def _base_ctx(request: Request, user: User, active_page: str) -> dict:
    """Build the base template context with user info."""
    return {
        "request": request,
        "active_page": active_page,
        "user": user,
    }


@router.get("/login")
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _try_get_user(request, db)
    if user:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/")
async def dashboard(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    view_user_id = request.query_params.get("view_user_id")
    view_user_id = int(view_user_id) if view_user_id is not None else None
    uid = get_effective_user_id(user, view_user_id)

    # Sources with their latest task info
    sources_q_stmt = select(MonitorSource).order_by(MonitorSource.id)
    if uid is not None:
        sources_q_stmt = sources_q_stmt.where(MonitorSource.user_id == uid)
    sources_q = await db.execute(sources_q_stmt)
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

    results_stmt = select(func.count(CrawlResult.id))
    if uid is not None:
        results_stmt = results_stmt.where(CrawlResult.user_id == uid)
    total_results_q = await db.execute(results_stmt)
    total_results = total_results_q.scalar() or 0

    reports_stmt = select(func.count(Report.id))
    if uid is not None:
        reports_stmt = reports_stmt.where(Report.user_id == uid)
    total_reports_q = await db.execute(reports_stmt)
    total_reports = total_reports_q.scalar() or 0

    last_task_stmt = select(CrawlTask).order_by(CrawlTask.completed_at.desc()).limit(1)
    if uid is not None:
        last_task_stmt = last_task_stmt.where(CrawlTask.user_id == uid)
    last_task_q = await db.execute(last_task_stmt)
    last_task = last_task_q.scalar_one_or_none()
    last_crawl = str(last_task.completed_at)[:16] if last_task and last_task.completed_at else None

    ctx = _base_ctx(request, user, "dashboard")
    ctx.update({
        "sources": source_data,
        "stats": {
            "total_sources": total_sources,
            "active_sources": active_sources,
            "total_results": total_results,
            "total_reports": total_reports,
            "last_crawl": last_crawl,
        },
    })
    return templates.TemplateResponse("dashboard.html", ctx)


@router.get("/sources")
async def sources_page(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    view_user_id = request.query_params.get("view_user_id")
    view_user_id = int(view_user_id) if view_user_id is not None else None
    uid = get_effective_user_id(user, view_user_id)

    stmt = select(MonitorSource).order_by(MonitorSource.id)
    if uid is not None:
        stmt = stmt.where(MonitorSource.user_id == uid)
    result = await db.execute(stmt)
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
    ctx = _base_ctx(request, user, "sources")
    ctx.update({
        "sources": sources,
        "default_crawl_rules": DEFAULT_CRAWL_RULES,
    })
    return templates.TemplateResponse("sources.html", ctx)


@router.get("/reports")
async def reports_page(request: Request, user: User = Depends(get_current_user)):
    ctx = _base_ctx(request, user, "reports")
    return templates.TemplateResponse("reports.html", ctx)


@router.get("/reports/{report_id}")
async def report_detail_page(report_id: int, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    report = await db.get(Report, report_id)
    if not report:
        ctx = _base_ctx(request, user, "reports")
        ctx["reports"] = []
        return templates.TemplateResponse("reports.html", ctx)
    # Access check: user can only see own reports, admin can see all
    if user.role != "admin" and report.user_id != user.id:
        raise HTTPException(403, "无权限查看此报告")
    ctx = _base_ctx(request, user, "reports")
    ctx["report"] = {
        "id": report.id,
        "title": report.title,
        "content_html": report.content_html,
        "overview": report.overview,
        "generated_at": str(report.generated_at)[:16] if report.generated_at else None,
    }
    return templates.TemplateResponse("report_detail.html", ctx)


@router.get("/tasks")
async def tasks_page(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    view_user_id = request.query_params.get("view_user_id")
    view_user_id = int(view_user_id) if view_user_id is not None else None
    uid = get_effective_user_id(user, view_user_id)

    stmt = select(CrawlTask).order_by(CrawlTask.created_at.desc()).limit(100)
    if uid is not None:
        stmt = stmt.where(CrawlTask.user_id == uid)
    result = await db.execute(stmt)
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
    ctx = _base_ctx(request, user, "tasks")
    ctx["tasks"] = tasks
    return templates.TemplateResponse("tasks.html", ctx)


@router.get("/push-rules")
async def push_rules_page(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    view_user_id = request.query_params.get("view_user_id")
    view_user_id = int(view_user_id) if view_user_id is not None else None
    uid = get_effective_user_id(user, view_user_id)

    rules_stmt = select(PushRule).order_by(PushRule.id)
    if uid is not None:
        rules_stmt = rules_stmt.where(PushRule.user_id == uid)
    rules_q = await db.execute(rules_stmt)

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

    ctx = _base_ctx(request, user, "push_rules")
    ctx["rules"] = rules
    return templates.TemplateResponse("push_rules.html", ctx)


@router.get("/settings")
async def settings_page(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    # Settings page is admin-only
    if user.role != "admin":
        raise HTTPException(403, "需要管理员权限")

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

    ctx = _base_ctx(request, user, "settings")
    ctx.update({
        "llm": llm_data,
        "email": email_data,
        "scheduler_jobs": get_scheduler_jobs(),
    })
    return templates.TemplateResponse("settings.html", ctx)


@router.get("/accounts")
async def accounts_page(request: Request, user: User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    ctx = _base_ctx(request, user, "accounts")
    return templates.TemplateResponse("accounts.html", ctx)
