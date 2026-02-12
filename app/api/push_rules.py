"""Push rule management API."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.push_rule import PushRule
from app.models.user import User
from app.auth import get_current_user, get_effective_user_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/push-rules", tags=["push_rules"])


class PushRuleCreate(BaseModel):
    name: str
    source_ids: list[int] = []
    channel: str = "email"
    recipients: list[str] = []
    push_mode: str = "on_update"
    push_schedule: str = ""
    is_active: bool = True


class PushRuleUpdate(BaseModel):
    name: str | None = None
    source_ids: list[int] | None = None
    channel: str | None = None
    recipients: list[str] | None = None
    push_mode: str | None = None
    push_schedule: str | None = None
    is_active: bool | None = None


@router.get("")
async def list_rules(
    view_user_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    uid = get_effective_user_id(user, view_user_id)
    stmt = select(PushRule).order_by(PushRule.id)
    if uid is not None:
        stmt = stmt.where(PushRule.user_id == uid)
    result = await db.execute(stmt)
    rules = result.scalars().all()
    return [_to_dict(r) for r in rules]


@router.post("")
async def create_rule(data: PushRuleCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rule = PushRule(**data.model_dump())
    rule.user_id = user.id
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    # Sync scheduler if scheduled mode
    from app.scheduler.scheduler import sync_push_schedules
    await sync_push_schedules()
    return _to_dict(rule)


@router.put("/{rule_id}")
async def update_rule(rule_id: int, data: PushRuleUpdate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rule = await db.get(PushRule, rule_id)
    if not rule:
        raise HTTPException(404, "推送规则不存在")
    if user.role != "admin" and rule.user_id != user.id:
        raise HTTPException(403, "无权限")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(rule, key, value)
    await db.commit()
    await db.refresh(rule)
    # Sync scheduler if schedule changed
    from app.scheduler.scheduler import sync_push_schedules
    await sync_push_schedules()
    return _to_dict(rule)


@router.delete("/{rule_id}")
async def delete_rule(rule_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rule = await db.get(PushRule, rule_id)
    if not rule:
        raise HTTPException(404, "推送规则不存在")
    if user.role != "admin" and rule.user_id != user.id:
        raise HTTPException(403, "无权限")
    await db.delete(rule)
    await db.commit()
    # Sync scheduler after deletion
    from app.scheduler.scheduler import sync_push_schedules
    await sync_push_schedules()
    return {"ok": True}


@router.post("/{rule_id}/push")
async def push_now(rule_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Immediately push the latest report for a rule."""
    from app.models.report import Report
    from app.notification.email_sender import send_email

    rule = await db.get(PushRule, rule_id)
    if not rule:
        raise HTTPException(404, "推送规则不存在")
    if user.role != "admin" and rule.user_id != user.id:
        raise HTTPException(403, "无权限")

    # Get the latest report for this user
    report_stmt = select(Report).order_by(Report.generated_at.desc()).limit(1)
    if user.role != "admin":
        report_stmt = report_stmt.where(Report.user_id == user.id)
    result = await db.execute(report_stmt)
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "暂无报告可推送，请先执行一次采集")

    if rule.channel == "email":
        recipients = rule.recipients or []
        if not recipients:
            raise HTTPException(400, "该规则未设置接收人")
        await send_email(
            recipients=recipients,
            subject=f"[政策情报] {report.title}",
            html_body=report.content_html,
            text_body=report.content_text,
        )
        logger.info("Manual push sent to %s via rule '%s'", recipients, rule.name)
        return {"ok": True, "message": f"已推送至 {len(recipients)} 个接收人"}
    else:
        raise HTTPException(400, f"渠道 {rule.channel} 暂不支持立即推送")


def _to_dict(r: PushRule) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "source_ids": r.source_ids,
        "channel": r.channel,
        "recipients": r.recipients,
        "push_mode": r.push_mode,
        "push_schedule": r.push_schedule,
        "is_active": r.is_active,
        "created_at": str(r.created_at) if r.created_at else None,
    }
