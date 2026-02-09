from datetime import datetime, date
from sqlalchemy import Integer, String, Text, Boolean, DateTime, Date
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class CrawlResult(Base):
    __tablename__ = "crawl_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_type: Mapped[str] = mapped_column(String(50), default="news")  # news/policy/notice/file
    summary: Mapped[str] = mapped_column(Text, default="")
    original_text: Mapped[str] = mapped_column(Text, default="")
    has_attachment: Mapped[bool] = mapped_column(Boolean, default=False)
    attachment_name: Mapped[str] = mapped_column(String(500), default="")
    attachment_type: Mapped[str] = mapped_column(String(20), default="")  # pdf/doc/docx/xlsx
    attachment_path: Mapped[str] = mapped_column(String(1000), default="")
    attachment_summary: Mapped[str] = mapped_column(Text, default="")
    published_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    crawled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
