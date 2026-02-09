"""Push notification engine: dispatches reports according to push rules."""
import logging

from sqlalchemy import select

from app.database.connection import async_session
from app.models.push_rule import PushRule
from app.models.result import CrawlResult
from app.notification.email_sender import send_email

logger = logging.getLogger(__name__)


async def dispatch_report(
    batch_id: str,
    title: str,
    content_html: str,
    content_text: str,
    results: list[CrawlResult],
):
    """Dispatch the report to all matching push rules."""
    async with async_session() as session:
        q = await session.execute(
            select(PushRule).where(PushRule.is_active == True)
        )
        rules = list(q.scalars().all())

    if not rules:
        logger.info("No active push rules, skipping dispatch")
        return

    # Collect source_ids present in this batch
    batch_source_ids = set(r.source_id for r in results)

    for rule in rules:
        # Only auto-dispatch for on_update rules (skip scheduled-only rules)
        if getattr(rule, 'push_mode', 'on_update') == 'scheduled':
            logger.info("Skipping scheduled-only rule '%s'", rule.name)
            continue

        # Check if this rule matches any source in the batch
        rule_source_ids = set(rule.source_ids) if rule.source_ids else set()

        # Empty source_ids means "all sources"
        if rule_source_ids and not rule_source_ids.intersection(batch_source_ids):
            continue

        if rule.channel == "email":
            recipients = rule.recipients or []
            if recipients:
                # If rule has specific source_ids, filter content
                # For MVP, send the full report
                await send_email(
                    recipients=recipients,
                    subject=f"[政策情报] {title}",
                    html_body=content_html,
                    text_body=content_text,
                )
                logger.info("Dispatched email to %s via rule '%s'", recipients, rule.name)

        elif rule.channel == "wechat_webhook":
            # V2 feature — placeholder
            logger.info("WeChat webhook not implemented yet, skipping rule '%s'", rule.name)
