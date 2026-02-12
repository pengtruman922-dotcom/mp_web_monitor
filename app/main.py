"""FastAPI application entry point."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import APP_HOST, APP_PORT
from app.database.connection import init_db
from app.database.migrations import seed_default_data
from app.scheduler.scheduler import init_scheduler

# Import all models so SQLAlchemy knows about them
from app.models import source, task, result, report, push_rule, settings, user  # noqa: F401

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
from app.api.auth import router as auth_router
from app.api.users import router as users_router
from app.api.sources import router as sources_router
from app.api.tasks import router as tasks_router
from app.api.reports import router as reports_router
from app.api.results import router as results_router
from app.api.push_rules import router as push_rules_router
from app.api.settings import router as settings_router

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(sources_router)
app.include_router(tasks_router)
app.include_router(reports_router)
app.include_router(results_router)
app.include_router(push_rules_router)
app.include_router(settings_router)

# Register web page routes
from app.web.routes import router as web_router
app.include_router(web_router)


# --- 401 exception handler ---
from fastapi.exceptions import HTTPException as FastAPIHTTPException  # noqa: E402


@app.exception_handler(401)
async def unauthorized_handler(request: Request, exc: FastAPIHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": str(exc.detail)})
    return RedirectResponse(url="/login", status_code=302)


def main():
    import socket
    import uvicorn

    port = APP_PORT
    max_tries = 10
    for i in range(max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                break  # port is free
            logging.getLogger(__name__).warning("端口 %d 已被占用，尝试 %d ...", port, port + 1)
            port += 1
    else:
        logging.getLogger(__name__).error("端口 %d-%d 均被占用，无法启动", APP_PORT, APP_PORT + max_tries - 1)
        return

    if port != APP_PORT:
        logging.getLogger(__name__).info("使用备用端口 %d 启动", port)
    # reload=False: hot-reload kills background tasks (agent crawling)
    uvicorn.run("app.main:app", host=APP_HOST, port=port, reload=False)


if __name__ == "__main__":
    main()
