"""APScheduler-based task scheduler."""
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.database.connection import async_session
from app.models.source import MonitorSource
from app.models.task import TriggerType

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def _scheduled_crawl():
    """Callback for scheduled crawl: run all active sources, grouped by user."""
    from app.agent.orchestrator import run_batch
    from app.models.user import User
    from sqlalchemy import distinct

    logger.info("Scheduled crawl triggered")
    try:
        # Find all user_ids that have active sources
        async with async_session() as session:
            result = await session.execute(
                select(distinct(MonitorSource.user_id)).where(MonitorSource.is_active == True)
            )
            user_ids = [row[0] for row in result.all()]

        for uid in user_ids:
            try:
                await run_batch(triggered_by=TriggerType.scheduled.value, user_id=uid)
            except Exception as e:
                logger.error("Scheduled crawl failed for user %d: %s", uid, e)
    except Exception as e:
        logger.error("Scheduled crawl failed: %s", e)


async def _scheduled_push(rule_id: int):
    """Callback for scheduled push: send latest report for a specific rule."""
    from app.models.push_rule import PushRule
    from app.models.report import Report
    from app.notification.email_sender import send_email

    logger.info("Scheduled push triggered for rule %d", rule_id)
    try:
        async with async_session() as session:
            rule = await session.get(PushRule, rule_id)
            if not rule or not rule.is_active:
                logger.warning("Push rule %d not found or inactive, skipping", rule_id)
                return

            # Get the latest report
            result = await session.execute(
                select(Report).order_by(Report.generated_at.desc()).limit(1)
            )
            report = result.scalar_one_or_none()
            if not report:
                logger.info("No report available for scheduled push (rule %d)", rule_id)
                return

        if rule.channel == "email":
            recipients = rule.recipients or []
            if recipients:
                await send_email(
                    recipients=recipients,
                    subject=f"[政策情报] {report.title}",
                    html_body=report.content_html,
                    text_body=report.content_text,
                )
                logger.info("Scheduled push sent to %s via rule '%s'", recipients, rule.name)
        else:
            logger.info("Channel %s not supported for scheduled push yet", rule.channel)

    except Exception as e:
        logger.error("Scheduled push for rule %d failed: %s", rule_id, e)


async def sync_push_schedules():
    """Sync push rule scheduled jobs with the scheduler.

    Called on startup and whenever push rules are created/updated/deleted.
    """
    from app.models.push_rule import PushRule

    # Remove all existing push_ jobs
    for job in scheduler.get_jobs():
        if job.id.startswith("push_rule_"):
            scheduler.remove_job(job.id)

    # Add jobs for active scheduled rules
    try:
        async with async_session() as session:
            result = await session.execute(
                select(PushRule).where(
                    PushRule.is_active == True,
                    PushRule.push_mode == "scheduled",
                )
            )
            rules = list(result.scalars().all())

        for rule in rules:
            if not rule.push_schedule:
                continue
            try:
                scheduler.add_job(
                    _scheduled_push,
                    CronTrigger.from_crontab(rule.push_schedule),
                    args=[rule.id],
                    id=f"push_rule_{rule.id}",
                    replace_existing=True,
                    name=f"定时推送: {rule.name}",
                )
                logger.info("Registered scheduled push job for rule '%s' (%s)",
                            rule.name, rule.push_schedule)
            except Exception as e:
                logger.error("Invalid cron for push rule '%s': %s", rule.name, e)

    except Exception as e:
        logger.error("Failed to sync push schedules: %s", e)


async def init_scheduler():
    """Initialize the scheduler with jobs from database source schedules."""
    # Add a default weekly job (Monday 9am) that crawls all active sources
    scheduler.add_job(
        _scheduled_crawl,
        CronTrigger.from_crontab("0 9 * * 1"),  # Every Monday 9:00 AM
        id="default_weekly_crawl",
        replace_existing=True,
        name="每周一9:00自动采集",
    )
    scheduler.start()
    logger.info("Scheduler started with default weekly job")

    # Sync push rule schedules
    await sync_push_schedules()


async def update_schedule(cron_expression: str):
    """Update the default scheduled job with a new cron expression."""
    try:
        scheduler.reschedule_job(
            "default_weekly_crawl",
            trigger=CronTrigger.from_crontab(cron_expression),
        )
        logger.info("Schedule updated to: %s", cron_expression)
    except Exception as e:
        logger.error("Failed to update schedule: %s", e)
        raise


def get_scheduler_jobs() -> list[dict]:
    """Return info about current scheduler jobs."""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": str(job.next_run_time) if job.next_run_time else "未调度",
            "trigger": str(job.trigger),
        })
    return jobs
