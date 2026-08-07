import os


def _int(name, default, minimum=1):
    try:
        return max(int(os.getenv(name, str(default))), minimum)
    except (TypeError, ValueError):
        return default


APP_PORT = _int("APP_PORT", 8080, minimum=0)
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "uploader.db")
LOG_PATH = os.path.join(DATA_DIR, "uploader.log")

SCAN_INTERVAL = _int("SCAN_INTERVAL", 30)
STABLE_SECONDS = _int("STABLE_SECONDS", 30)
UPLOAD_CONCURRENCY = _int("UPLOAD_CONCURRENCY", 2)
RETRY_MAX = _int("RETRY_MAX", 3)
RETRY_BACKOFF = _int("RETRY_BACKOFF", 30, minimum=0)

# Delete the local file from disk after it has been uploaded successfully
# to ALL enabled providers, to save disk space.
DELETE_AFTER_UPLOAD = os.getenv("DELETE_AFTER_UPLOAD", "true").lower() == "true"

AUTH_USER = os.getenv("AUTH_USER", "")
AUTH_PASS = os.getenv("AUTH_PASS", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

INCOMPLETE_SUFFIXES = (
    ".part", ".partial", ".incomplete", ".!qb", ".!qbr", ".aria2",
    ".crdownload", ".opdownload", ".download", ".tmp", ".torrent",
)
INCOMPLETE_DIR_MARKERS = ("!qb", ".incomplete", "partial", "temp")
