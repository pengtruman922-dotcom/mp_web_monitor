"""System settings API: LLM config, email config, scheduler."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.settings import LLMConfig, EmailConfig
from app.models.user import User
from app.auth import require_admin
from app.scheduler.scheduler import update_schedule, get_scheduler_jobs

router = APIRouter(prefix="/api/settings", tags=["settings"])


# --- LLM Config ---

class LLMConfigUpdate(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    name: str
    api_url: str
    api_key: str
    model_name: str


@router.get("/llm")
async def get_llm_config(db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    result = await db.execute(select(LLMConfig).where(LLMConfig.is_active == True).limit(1))
    config = result.scalar_one_or_none()
    if not config:
        return None
    return {
        "id": config.id,
        "name": config.name,
        "api_url": config.api_url,
        "api_key": _mask_key(config.api_key),
        "model_name": config.model_name,
        "is_active": config.is_active,
    }


@router.put("/llm")
async def update_llm_config(data: LLMConfigUpdate, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    result = await db.execute(select(LLMConfig).where(LLMConfig.is_active == True).limit(1))
    config = result.scalar_one_or_none()
    if config:
        config.name = data.name
        config.api_url = data.api_url
        if not data.api_key.startswith("sk-***"):  # Only update if not masked
            config.api_key = data.api_key
        config.model_name = data.model_name
    else:
        config = LLMConfig(
            name=data.name,
            api_url=data.api_url,
            api_key=data.api_key,
            model_name=data.model_name,
            is_active=True,
        )
        db.add(config)
    await db.commit()
    return {"ok": True}


@router.post("/llm/test")
async def test_llm_config(data: LLMConfigUpdate, admin: User = Depends(require_admin)):
    """Test LLM connectivity."""
    from openai import AsyncOpenAI

    base_url = data.api_url
    for suffix in ["/chat/completions", "/chat"]:
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)]
            break

    client = AsyncOpenAI(api_key=data.api_key, base_url=base_url)
    try:
        response = await client.chat.completions.create(
            model=data.model_name,
            messages=[{"role": "user", "content": "请回复'连接成功'四个字"}],
            max_tokens=20,
        )
        content = response.choices[0].message.content
        return {"ok": True, "response": content}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- Email Config ---

class EmailConfigUpdate(BaseModel):
    smtp_host: str
    smtp_port: int = 465
    use_tls: bool = True
    username: str = ""
    password: str = ""
    sender_email: str = ""
    sender_name: str = "政策情报助手"


@router.get("/email")
async def get_email_config(db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    result = await db.execute(select(EmailConfig).limit(1))
    config = result.scalar_one_or_none()
    if not config:
        return None
    return {
        "id": config.id,
        "smtp_host": config.smtp_host,
        "smtp_port": config.smtp_port,
        "use_tls": config.use_tls,
        "username": config.username,
        "password": "***" if config.password else "",
        "sender_email": config.sender_email,
        "sender_name": config.sender_name,
    }


@router.put("/email")
async def update_email_config(data: EmailConfigUpdate, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    result = await db.execute(select(EmailConfig).limit(1))
    config = result.scalar_one_or_none()
    if config:
        config.smtp_host = data.smtp_host
        config.smtp_port = data.smtp_port
        config.use_tls = data.use_tls
        config.username = data.username
        if data.password != "***":
            config.password = data.password
        config.sender_email = data.sender_email
        config.sender_name = data.sender_name
    else:
        config = EmailConfig(**data.model_dump())
        db.add(config)
    await db.commit()
    return {"ok": True}


# --- Scheduler ---

class ScheduleUpdate(BaseModel):
    cron: str  # e.g. "0 9 * * 1"


@router.get("/scheduler")
async def get_schedule(admin: User = Depends(require_admin)):
    return {"jobs": get_scheduler_jobs()}


@router.put("/scheduler")
async def update_scheduler(data: ScheduleUpdate, admin: User = Depends(require_admin)):
    try:
        await update_schedule(data.cron)
        return {"ok": True, "cron": data.cron}
    except Exception as e:
        raise HTTPException(400, f"无效的Cron表达式: {e}")


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return key[:3] + "***" + key[-4:]
