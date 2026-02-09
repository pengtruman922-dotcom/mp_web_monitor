import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.config import DATABASE_URL

logger = logging.getLogger(__name__)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency that provides a database session."""
    async with async_session() as session:
        yield session


# Column migrations: (table, column, column_definition)
_COLUMN_MIGRATIONS = [
    ("monitor_sources", "time_range_days", "INTEGER DEFAULT 7"),
    ("monitor_sources", "max_items", "INTEGER DEFAULT 30"),
    ("reports", "overview", "TEXT DEFAULT ''"),
    ("crawl_tasks", "progress_log", "TEXT DEFAULT ''"),
    ("push_rules", "push_mode", "VARCHAR(20) DEFAULT 'on_update'"),
    ("push_rules", "push_schedule", "VARCHAR(100) DEFAULT ''"),
    ("monitor_sources", "crawl_rules", "TEXT DEFAULT ''"),
]


async def _run_migrations(conn):
    """Add missing columns to existing tables (SQLite compatible)."""
    for table, column, col_def in _COLUMN_MIGRATIONS:
        try:
            await conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN {column} {col_def}"
            ))
            logger.info("Migration: added column %s.%s", table, column)
        except Exception:
            pass  # Column already exists, skip silently


async def init_db():
    """Create all tables and run column migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _run_migrations(conn)
