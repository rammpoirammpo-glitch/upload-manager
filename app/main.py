import base64
import logging
import logging.handlers
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
        # Rotating file handler: the log can never grow without bound on a
        # 24/7 install (disk-space guard).
        handlers.append(logging.handlers.RotatingFileHandler(
            config.LOG_PATH, maxBytes=config.LOG_MAX_BYTES,
            backupCount=config.LOG_BACKUPS, encoding="utf-8"))
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
    # Bridge the logging system to Telegram: every WARNING/ERROR record is
    # forwarded (rate-limited) so the operator is alerted while away.
    root_logger = logging.getLogger()
    root_logger.addHandler(notifier.log_handler)
    watcher.start()
    scheduler.start()
    logger.info("Upload Manager started: %d pending item(s), %d enabled provider(s)",
                pending, providers)
    yield
    watcher.running = False
    scheduler.running = False
    watcher.join(timeout=5)
    # Give the scheduler a moment to persist the final state of in-flight items
    # before the process exits.
    scheduler.join(timeout=15)
    root_logger.removeHandler(notifier.log_handler)
    notifier.shutdown()


app = FastAPI(title="Upload Manager", version="1.5.2", lifespan=lifespan)
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
                                headers={"WWW-Authenticate": "Basic"}) from None
        # compare_digest only accepts ASCII str, so hash both sides to bytes
        # first: non-ASCII passwords must not crash the request (500) or
        # bypass the check.
        def _eq(a, b):
            try:
                return secrets.compare_digest(a, b)
            except TypeError:
                return secrets.compare_digest(
                    str(a).encode("utf-8", "replace"),
                    str(b).encode("utf-8", "replace"))

        if not (_eq(user, config.AUTH_USER) and _eq(password, config.AUTH_PASS)):
            raise HTTPException(401, "Unauthorized",
                                headers={"WWW-Authenticate": "Basic"})
        return await call_next(request)

_static = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static):
    app.mount("/", StaticFiles(directory=_static, html=True), name="static")
