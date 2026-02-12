from datetime import datetime
from sqlalchemy import Integer, String, Text, Boolean, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class MonitorSource(Base):
    __tablename__ = "monitor_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    focus_areas: Mapped[dict] = mapped_column(JSON, default=list)
    max_depth: Mapped[int] = mapped_column(Integer, default=3)
    content_types: Mapped[dict] = mapped_column(JSON, default=lambda: ["news", "policy", "notice", "file"])
    schedule: Mapped[str] = mapped_column(String(100), default="0 9 * * 1")  # Every Monday 9am
    time_range_days: Mapped[int] = mapped_column(Integer, default=7)  # 1-90, collect content from last N days
    max_items: Mapped[int] = mapped_column(Integer, default=30)  # 10-50, max items per crawl
    crawl_rules: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
