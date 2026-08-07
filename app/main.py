import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from . import config, database
from .api import router
from .notifier import Notifier
from .queue import QueueScheduler
from .watcher import Watcher


def setup_logging():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(config.LOG_PATH, encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
    )


setup_logging()
logger = logging.getLogger("uploader")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    with database.connect() as conn:
        conn.execute(
            "UPDATE queue_items SET status='pending', updated_at=datetime('now') "
            "WHERE status='uploading'")
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM queue_items WHERE status='pending'").fetchone()["c"]
        providers = conn.execute(
            "SELECT COUNT(*) AS c FROM providers WHERE enabled=1").fetchone()["c"]
    notifier = Notifier()
    watcher = Watcher(notifier)
    scheduler = QueueScheduler(notifier)
    app.state.watcher = watcher
    app.state.scheduler = scheduler
    app.state.notifier = notifier
    watcher.start()
    scheduler.start()
    logger.info("Upload Manager started: %d pending item(s), %d enabled provider(s)",
                pending, providers)
    yield
    watcher.running = False
    scheduler.running = False
    watcher.join(timeout=5)
    scheduler.join(timeout=5)


app = FastAPI(title="Upload Manager", version="1.0.0", lifespan=lifespan)
app.include_router(router)


if config.AUTH_USER:
    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            raise HTTPException(401, "Unauthorized",
                                headers={"WWW-Authenticate": "Basic"})
        try:
            token = base64.b64decode(auth.split(" ", 1)[1]).decode()
            user, _, password = token.partition(":")
        except Exception:
            raise HTTPException(401, "Unauthorized",
                                headers={"WWW-Authenticate": "Basic"})
        if not (secrets.compare_digest(user, config.AUTH_USER)
                and secrets.compare_digest(password, config.AUTH_PASS)):
            raise HTTPException(401, "Unauthorized",
                                headers={"WWW-Authenticate": "Basic"})
        return await call_next(request)

_static = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static):
    app.mount("/", StaticFiles(directory=_static, html=True), name="static")
