"""Database initialization and default data seeding."""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import async_session
from app.models.source import MonitorSource
from app.models.user import User
from app.models.settings import LLMConfig, EmailConfig, PromptConfig
from app.config import (
    DEFAULT_LLM_API_URL, DEFAULT_LLM_API_KEY, DEFAULT_LLM_MODEL_NAME,
    DEFAULT_SMTP_HOST, DEFAULT_SMTP_PORT, DEFAULT_SMTP_USE_TLS,
    DEFAULT_SMTP_USERNAME, DEFAULT_SMTP_PASSWORD,
    DEFAULT_SENDER_EMAIL, DEFAULT_SENDER_NAME,
)

_logger = logging.getLogger(__name__)

# Default monitor sources for MVP
DEFAULT_SOURCES = [
    {
        "name": "国家能源局",
        "url": "https://www.nea.gov.cn/",
        "description": "国家能源局官方网站，发布能源政策、行业数据、通知公告等",
        "focus_areas": ["政策法规", "通知公告", "工作动态"],
        "max_depth": 3,
        "content_types": ["news", "policy", "notice", "file"],
        "schedule": "0 9 * * 1",
        "time_range_days": 7,
        "max_items": 30,
    },
    {
        "name": "国务院国资委",
        "url": "http://www.sasac.gov.cn/",
        "description": "国务院国有资产监督管理委员会官方网站，发布国企改革、国资监管政策",
        "focus_areas": ["政策发布", "国企改革", "通知公告"],
        "max_depth": 3,
        "content_types": ["news", "policy", "notice", "file"],
        "schedule": "0 9 * * 1",
        "time_range_days": 7,
        "max_items": 30,
    },
    {
        "name": "共产党员网",
        "url": "https://www.12371.cn/",
        "description": "共产党员网，发布党建动态、政策学习、重要文件等",
        "focus_areas": ["时事政治", "政策文件", "学习资料"],
        "max_depth": 3,
        "content_types": ["news", "policy", "notice", "file"],
        "schedule": "0 9 * * 1",
        "time_range_days": 7,
        "max_items": 30,
    },
]


async def seed_default_data():
    """Insert default data if tables are empty."""
    async with async_session() as session:
        await _seed_admin_user(session)
        await _seed_sources(session)
        await _seed_llm_config(session)
        await _seed_email_config(session)
        await _seed_prompt_config(session)
        await session.commit()


async def _seed_admin_user(session: AsyncSession):
    result = await session.execute(select(User).limit(1))
    if result.scalar_one_or_none() is not None:
        return
    from app.auth import hash_password
    admin = User(
        username="admin",
        display_name="管理员",
        password_hash=hash_password("admin123"),
        role="admin",
        is_active=True,
        must_change_password=True,
    )
    session.add(admin)
    await session.flush()  # ensure id=1
    _logger.info("Seeded default admin user (admin/admin123)")


async def _seed_sources(session: AsyncSession):
    result = await session.execute(select(MonitorSource).limit(1))
    if result.scalar_one_or_none() is not None:
        return
    for src in DEFAULT_SOURCES:
        session.add(MonitorSource(**src))


async def _seed_llm_config(session: AsyncSession):
    result = await session.execute(select(LLMConfig).limit(1))
    if result.scalar_one_or_none() is not None:
        return
    if DEFAULT_LLM_API_URL and DEFAULT_LLM_API_KEY:
        session.add(LLMConfig(
            name=f"通义千问 {DEFAULT_LLM_MODEL_NAME}",
            api_url=DEFAULT_LLM_API_URL,
            api_key=DEFAULT_LLM_API_KEY,
            model_name=DEFAULT_LLM_MODEL_NAME,
            is_active=True,
        ))


async def _seed_email_config(session: AsyncSession):
    result = await session.execute(select(EmailConfig).limit(1))
    if result.scalar_one_or_none() is not None:
        return
    session.add(EmailConfig(
        smtp_host=DEFAULT_SMTP_HOST,
        smtp_port=DEFAULT_SMTP_PORT,
        use_tls=DEFAULT_SMTP_USE_TLS,
        username=DEFAULT_SMTP_USERNAME,
        password=DEFAULT_SMTP_PASSWORD,
        sender_email=DEFAULT_SENDER_EMAIL,
        sender_name=DEFAULT_SENDER_NAME,
    ))


async def _seed_prompt_config(session: AsyncSession):
    result = await session.execute(select(PromptConfig).limit(1))
    if result.scalar_one_or_none() is not None:
        return
    from app.agent.prompts import DEFAULT_TEMPLATE
    session.add(PromptConfig(
        name="默认提示词",
        template=DEFAULT_TEMPLATE,
        is_active=True,
    ))
