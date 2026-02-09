from datetime import datetime
from sqlalchemy import Integer, String, Boolean, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class PushRule(Base):
    __tablename__ = "push_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_ids: Mapped[list] = mapped_column(JSON, default=list)  # List of source IDs
    channel: Mapped[str] = mapped_column(String(50), default="email")  # email / wechat_webhook
    recipients: Mapped[list] = mapped_column(JSON, default=list)  # Email addresses or webhook URLs
    push_mode: Mapped[str] = mapped_column(String(20), default="on_update")  # on_update / scheduled
    push_schedule: Mapped[str] = mapped_column(String(100), default="")  # cron expression (when scheduled)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
