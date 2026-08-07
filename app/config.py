import os

APP_PORT = int(os.getenv("APP_PORT", "8080"))
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "uploader.db")
LOG_PATH = os.path.join(DATA_DIR, "uploader.log")

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))
STABLE_SECONDS = int(os.getenv("STABLE_SECONDS", "30"))
UPLOAD_CONCURRENCY = int(os.getenv("UPLOAD_CONCURRENCY", "2"))
RETRY_MAX = int(os.getenv("RETRY_MAX", "3"))
RETRY_BACKOFF = int(os.getenv("RETRY_BACKOFF", "30"))

AUTH_USER = os.getenv("AUTH_USER", "")
AUTH_PASS = os.getenv("AUTH_PASS", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

INCOMPLETE_SUFFIXES = (
    ".part", ".partial", ".incomplete", ".!qb", ".!qbr", ".aria2",
    ".crdownload", ".opdownload", ".download", ".tmp", ".torrent",
)
INCOMPLETE_DIR_MARKERS = ("!qb", ".incomplete", "partial", "temp")
