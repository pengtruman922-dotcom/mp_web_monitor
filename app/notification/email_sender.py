"""Email notification sender using aiosmtplib."""
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import aiosmtplib
from sqlalchemy import select

from app.database.connection import async_session
from app.models.settings import EmailConfig

logger = logging.getLogger(__name__)


async def get_email_config() -> EmailConfig | None:
    async with async_session() as session:
        result = await session.execute(select(EmailConfig).limit(1))
        return result.scalar_one_or_none()


async def send_email(recipients: list[str], subject: str, html_body: str, text_body: str = "") -> bool:
    """Send an HTML email to a list of recipients.

    Returns True if sent successfully, False otherwise.
    """
    config = await get_email_config()
    if not config or not config.smtp_host or not config.sender_email:
        logger.warning("Email not configured, skipping send")
        return False

    if not recipients:
        logger.warning("No recipients specified, skipping send")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{config.sender_name} <{config.sender_email}>"
    msg["To"] = ", ".join(recipients)

    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        await aiosmtplib.send(
            msg,
            hostname=config.smtp_host,
            port=config.smtp_port,
            username=config.username if config.username else None,
            password=config.password if config.password else None,
            use_tls=config.use_tls,
        )
        logger.info("Email sent to %s: %s", recipients, subject)
        return True
    except Exception as e:
        logger.error("Failed to send email: %s", e)
        return False
