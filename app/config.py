"""Central configuration for the upload-manager daemon.

Every value can be overridden with an environment variable so the same image
works on umbrelOS and on a standalone VPS. Values are read once at startup
(they are constants for the process lifetime).
"""

import os


def _int(name, default, minimum=1):
    """Read an int env var, clamped to >= minimum; fall back to default."""
    try:
        return max(int(os.getenv(name, str(default))), minimum)
    except (TypeError, ValueError):
        return default


def _bool(name, default):
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes", "on")


APP_PORT = _int("APP_PORT", 8080, minimum=0)
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "uploader.db")
LOG_PATH = os.path.join(DATA_DIR, "uploader.log")

# --- Watcher -----------------------------------------------------------------
SCAN_INTERVAL = _int("SCAN_INTERVAL", 30)          # seconds between scans
STABLE_SECONDS = _int("STABLE_SECONDS", 30)        # file age before it is queued

# --- Queue / concurrency -----------------------------------------------------
UPLOAD_CONCURRENCY = _int("UPLOAD_CONCURRENCY", 2)  # items uploaded in parallel
RETRY_MAX = _int("RETRY_MAX", 3)                    # attempts per provider
RETRY_BACKOFF = _int("RETRY_BACKOFF", 30)           # base backoff (seconds)
RETRY_BACKOFF_CAP = _int("RETRY_BACKOFF_CAP", 300)  # never wait longer than this
RETRY_JITTER = _int("RETRY_JITTER", 5, minimum=0)   # +0..N random seconds

# --- Worker anti-stall (zero-freeze guard) -----------------------------------
# How long a worker thread may go without a progress heartbeat before the
# watchdog treats it as STUCK and reclaims its concurrency slot (resets the
# item to pending so a stalled provider can never wedge the queue forever on
# a 24/7 run). Combined with the per-future timeout in _process_item.
WORKER_STALL_SECONDS = _int("WORKER_STALL_SECONDS", 600, minimum=30)
# Timeout on the main _process_item thread while waiting for all provider
# worker futures to finish. If a single provider hangs (e.g. a botocore call
# that ignored its timeout), the item is marked failed / re-pended instead of
# staying 'uploading' and blocking a slot indefinitely.
WORKER_JOIN_TIMEOUT = _int("WORKER_JOIN_TIMEOUT", 1800, minimum=60)

# Completed items older than this are pruned automatically (0 = keep forever).
PRUNE_COMPLETED_DAYS = _int("PRUNE_COMPLETED_DAYS", 30, minimum=0)

# --- HTTP timeouts (used by every provider) ----------------------------------
# (connect, read) seconds. The read timeout is per chunk read, so a hung or
# stalled upload cannot freeze a worker forever, while still allowing slow
# (but alive) connections to make progress.
CONNECT_TIMEOUT = _int("CONNECT_TIMEOUT", 30, minimum=1)
READ_TIMEOUT = _int("READ_TIMEOUT", 300, minimum=10)

# --- Connection pooling (keep-alive) -----------------------------------------
# One requests.Session per provider instance with a connection pool, so
# sequential calls to the same host (upload-server fetch + upload, retries)
# reuse TCP/TLS connections instead of re-handshaking every time.
HTTP_POOL_CONNECTIONS = _int("HTTP_POOL_CONNECTIONS", 10, minimum=1)
HTTP_POOL_MAXSIZE = _int("HTTP_POOL_MAXSIZE", 20, minimum=1)

# --- High-speed S3 multipart -------------------------------------------------
# Large files are split into parts and uploaded in parallel (up to
# S3_MAX_CONCURRENCY parts at once) to saturate 1Gbps+ uplinks. Chunk size is
# clamped to >= 5MB (S3 minimum except for the last part).
S3_CHUNK_SIZE_MB = _int("S3_CHUNK_SIZE_MB", 16, minimum=5)
S3_MAX_CONCURRENCY = _int("S3_MAX_CONCURRENCY", 4, minimum=1)

# --- Per-item parallelism ----------------------------------------------------
# Max parallel provider workers spawned for a single item. Items are uploaded
# to several providers in parallel (UPLOAD_CONCURRENCY items at a time).
MAX_PROVIDERS_PER_ITEM = _int("MAX_PROVIDERS_PER_ITEM", 8, minimum=1)

# --- Log rotation (memory/disk guard on 24/7 runs) ---------------------------
LOG_MAX_BYTES = _int("LOG_MAX_BYTES", 5 * 1024 * 1024, minimum=1024)
LOG_BACKUPS = _int("LOG_BACKUPS", 5, minimum=0)

# Delete the local file from disk after it has been uploaded successfully
# to ALL enabled providers, to save disk space.
DELETE_AFTER_UPLOAD = _bool("DELETE_AFTER_UPLOAD", True)

# When a watch path has no "Remote folder" set, automatically use the watch
# path's own folder name (e.g. /downloads/movies -> "movies") as the cloud
# folder, so each source gets its own single cloud folder with no manual entry.
AUTO_REMOTE_FOLDER = _bool("AUTO_REMOTE_FOLDER", True)

# --- Dashboard auth ----------------------------------------------------------
AUTH_USER = os.getenv("AUTH_USER", "")
AUTH_PASS = os.getenv("AUTH_PASS", "")

# --- Telegram (legacy env fallback; the web UI settings take priority) -------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Incomplete-download detection -------------------------------------------
# Suffixes are trusted for BOTH files and directories. Directory-level markers
# (qBittorrent-style .!qb folders etc.) are matched precisely inside
# watcher.is_incomplete_name so ordinary names like "Temperature" are never
# skipped.
INCOMPLETE_SUFFIXES = (
    ".part", ".partial", ".incomplete", ".!qb", ".!qbr", ".aria2",
    ".crdownload", ".opdownload", ".download", ".tmp", ".torrent",
)
