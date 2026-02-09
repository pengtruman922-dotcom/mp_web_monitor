from datetime import datetime
from sqlalchemy import Integer, String, Boolean, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class LLMConfig(Base):
    __tablename__ = "llm_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    api_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str] = mapped_column(String(500), nullable=False)  # Encrypted
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EmailConfig(Base):
    __tablename__ = "email_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    smtp_host: Mapped[str] = mapped_column(String(200), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=465)
    use_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    username: Mapped[str] = mapped_column(String(200), default="")
    password: Mapped[str] = mapped_column(String(500), default="")  # Encrypted
    sender_email: Mapped[str] = mapped_column(String(200), default="")
    sender_name: Mapped[str] = mapped_column(String(200), default="政策情报助手")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PromptConfig(Base):
    __tablename__ = "prompt_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), default="默认提示词")
    template: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
