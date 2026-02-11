"""FastAPI application entry point."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import APP_HOST, APP_PORT
from app.database.connection import init_db
from app.database.migrations import seed_default_data
from app.scheduler.scheduler import init_scheduler

# Import all models so SQLAlchemy knows about them
from app.models import source, task, result, report, push_rule, settings  # noqa: F401

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def _cleanup_stale_tasks():
    """Mark any 'running' tasks as cancelled on startup (stale from previous crash)."""
    from app.database.connection import async_session
    from app.models.task import CrawlTask, TaskStatus
    from sqlalchemy import update
    async with async_session() as db:
        result = await db.execute(
            update(CrawlTask)
            .where(CrawlTask.status == TaskStatus.running.value)
            .values(status=TaskStatus.cancelled.value, error_log="服务器重启导致任务中断")
        )
        if result.rowcount:
            await db.commit()
            logging.getLogger(__name__).info("Cleaned up %d stale running tasks", result.rowcount)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    await seed_default_data()
    await _cleanup_stale_tasks()
    await init_scheduler()
    logging.getLogger(__name__).info("Application started")
    yield
    # Shutdown (cleanup if needed)


app = FastAPI(title="政策情报助手", version="1.0.0", lifespan=lifespan)

# Mount static files
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")

# Register API routers
from app.api.sources import router as sources_router
from app.api.tasks import router as tasks_router
from app.api.reports import router as reports_router
from app.api.results import router as results_router
from app.api.push_rules import router as push_rules_router
from app.api.settings import router as settings_router

app.include_router(sources_router)
app.include_router(tasks_router)
app.include_router(reports_router)
app.include_router(results_router)
app.include_router(push_rules_router)
app.include_router(settings_router)

# Register web page routes
from app.web.routes import router as web_router
app.include_router(web_router)


def main():
    import uvicorn
    # reload=False: hot-reload kills background tasks (agent crawling)
    uvicorn.run("app.main:app", host=APP_HOST, port=APP_PORT, reload=False)


if __name__ == "__main__":
    main()
