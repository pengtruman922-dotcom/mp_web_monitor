"""Push rule management API."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.push_rule import PushRule

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
async def list_rules(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PushRule).order_by(PushRule.id))
    rules = result.scalars().all()
    return [_to_dict(r) for r in rules]


@router.post("")
async def create_rule(data: PushRuleCreate, db: AsyncSession = Depends(get_db)):
    rule = PushRule(**data.model_dump())
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    # Sync scheduler if scheduled mode
    from app.scheduler.scheduler import sync_push_schedules
    await sync_push_schedules()
    return _to_dict(rule)


@router.put("/{rule_id}")
async def update_rule(rule_id: int, data: PushRuleUpdate, db: AsyncSession = Depends(get_db)):
    rule = await db.get(PushRule, rule_id)
    if not rule:
        raise HTTPException(404, "推送规则不存在")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(rule, key, value)
    await db.commit()
    await db.refresh(rule)
    # Sync scheduler if schedule changed
    from app.scheduler.scheduler import sync_push_schedules
    await sync_push_schedules()
    return _to_dict(rule)


@router.delete("/{rule_id}")
async def delete_rule(rule_id: int, db: AsyncSession = Depends(get_db)):
    rule = await db.get(PushRule, rule_id)
    if not rule:
        raise HTTPException(404, "推送规则不存在")
    await db.delete(rule)
    await db.commit()
    # Sync scheduler after deletion
    from app.scheduler.scheduler import sync_push_schedules
    await sync_push_schedules()
    return {"ok": True}


@router.post("/{rule_id}/push")
async def push_now(rule_id: int, db: AsyncSession = Depends(get_db)):
    """Immediately push the latest report for a rule."""
    from app.models.report import Report
    from app.notification.email_sender import send_email

    rule = await db.get(PushRule, rule_id)
    if not rule:
        raise HTTPException(404, "推送规则不存在")

    # Get the latest report
    result = await db.execute(
        select(Report).order_by(Report.generated_at.desc()).limit(1)
    )
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
