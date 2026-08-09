import os
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS watch_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    remote_dir TEXT NOT NULL DEFAULT '',
    provider_ids TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS queue_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    rel_path TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT '',
    remote_dir TEXT NOT NULL DEFAULT '',
    provider_ids TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    order_index INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS upload_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES queue_items(id) ON DELETE CASCADE,
    provider_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    progress REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(item_id, provider_id)
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_items(status);
CREATE INDEX IF NOT EXISTS idx_queue_folder ON queue_items(folder);
CREATE INDEX IF NOT EXISTS idx_tasks_item ON upload_tasks(item_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


class _DBConnection(sqlite3.Connection):
    """A connection used as a context manager that, on exit, commits or
    rolls back the transaction AND closes the connection.

    NOTE: sqlite3.Connection's own __exit__ only commits/rolls back but does
    NOT close the connection, which leaks a file descriptor per call. Over a
    24/7 run that exhausts the process FD limit and makes SQLite stop opening
    ("unable to open database file"). This subclass fixes exactly that.
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()


def connect(retries=5, delay=0.25):
    """Open a SQLite connection. A short retry loop smooths over transient
    'database is locked' / 'unable to open database file' errors under load."""
    last_exc = None
    for _ in range(max(1, retries)):
        try:
            conn = sqlite3.connect(
                config.DB_PATH,
                timeout=30,
                factory=_DBConnection,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except sqlite3.OperationalError as e:
            last_exc = e
            time.sleep(delay)
    raise last_exc


def init_db():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn):
    """Add columns introduced in later versions to pre-existing databases."""
    _add_column(conn, "watch_paths", "remote_dir", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "watch_paths", "provider_ids", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "queue_items", "remote_dir", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "queue_items", "provider_ids", "TEXT NOT NULL DEFAULT ''")


def _add_column(conn, table, column, definition):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def get_setting(key, default=""):
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value or ""))
