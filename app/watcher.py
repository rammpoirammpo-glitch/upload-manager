import logging
import os
import threading
import time

from . import config
from .database import connect

logger = logging.getLogger("uploader.watcher")


def is_incomplete_name(name, is_dir=False):
    """True if the entry looks like a partially-downloaded file/folder.

    For files we only trust file-extension suffixes (e.g. .part, .!qb): a
    naive substring test would wrongly skip legitimate names like
    "Temperature.mkv" (contains "temp") or "The.Partial.Mind.mkv"
    (contains "partial"). The broader marker test is only applied to
    directory names, where clients such as qBittorrent create folders like
    "Movie.Name.!qb" or "partial/" during a download.
    """
    low = name.lower()
    for suffix in config.INCOMPLETE_SUFFIXES:
        if low.endswith(suffix):
            return True
    if is_dir:
        # Directory-level download markers. Matched precisely so ordinary
        # folders like "Temperature" or "Partial" are never skipped.
        if ".!qb" in low or ".incomplete" in low:
            return True
        if low in ("partial", "temp", "incomplete", "tmp"):
            return True
    return False


class Watcher(threading.Thread):
    def __init__(self, notifier=None):
        super().__init__(daemon=True, name="watcher")
        self.running = True
        self._stable = {}
        self._stable_lock = threading.Lock()
        self._warned = set()
        self.notifier = notifier
        # Health counters surfaced by GET /api/status
        self.last_scan_at = None
        self.last_scan_duration = 0.0
        self.last_scan_result = {}
        self.scan_errors = 0

    def run(self):
        logger.info("Watcher started (scan every %ds)", config.SCAN_INTERVAL)
        while self.running:
            try:
                self.scan_once()
            except Exception:
                self.scan_errors += 1
                logger.exception("scan failed")
            time.sleep(config.SCAN_INTERVAL)

    def scan_once(self):
        started = time.time()
        result = {"paths": 0, "files": 0, "queued": 0}
        with connect() as conn:
            roots = [dict(r) for r in conn.execute(
                "SELECT path, remote_dir, provider_ids FROM watch_paths WHERE enabled=1")]
        seen = set()
        for root_rec in roots:
            root = os.path.abspath(root_rec["path"])
            remote_dir = root_rec["remote_dir"] or ""
            if not remote_dir and config.AUTO_REMOTE_FOLDER:
                remote_dir = os.path.basename(root.rstrip(os.sep)) or ""
            provider_ids = (root_rec["provider_ids"] or "").strip()
            if not os.path.isdir(root):
                if root not in self._warned:
                    logger.warning(
                        "Watch path does not exist inside the container: %s. "
                        "Check the volume mount in docker-compose.yml.", root)
                    self._warned.add(root)
                    if self.notifier:
                        self.notifier.notify(f"Watch path not found in container: {root}")
                continue
            result["paths"] += 1
            for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
                dirnames[:] = sorted(
                    d for d in dirnames
                    if not d.startswith(".") and not is_incomplete_name(d, is_dir=True))
                for name in sorted(filenames):
                    if name.startswith("."):
                        continue
                    if is_incomplete_name(name, is_dir=False):
                        continue
                    full = os.path.join(dirpath, name)
                    if not os.path.isfile(full):
                        continue
                    seen.add(full)
                    result["files"] += 1
                    if self._check(full, root, remote_dir, provider_ids):
                        result["queued"] += 1
        with self._stable_lock:
            stale = [p for p in self._stable if p not in seen]
            for p in stale:
                self._stable.pop(p, None)
        self.last_scan_at = time.time()
        self.last_scan_duration = time.time() - started
        self.last_scan_result = result
        return result

    def _check(self, path, root, remote_dir="", provider_ids=""):
        try:
            st = os.stat(path)
        except OSError:
            return False
        if st.st_size <= 0:
            return False
        now = time.time()
        age = now - st.st_mtime
        with self._stable_lock:
            prev = self._stable.get(path)
            if prev and prev[0] == st.st_size and prev[1] == st.st_mtime \
                    and (now - prev[2]) >= config.STABLE_SECONDS:
                self._stable.pop(path, None)
                return self._enqueue(path, root, st.st_size, remote_dir, provider_ids)
            if age >= config.STABLE_SECONDS:
                return self._enqueue(path, root, st.st_size, remote_dir, provider_ids)
            self._stable[path] = (st.st_size, st.st_mtime, now)
        return False

    def _enqueue(self, path, root, size, remote_dir="", provider_ids=""):
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            rel = os.path.basename(path)
        rel = rel.replace(os.sep, "/")
        rel_dir = os.path.dirname(rel)
        base = os.path.basename(root.rstrip("/")) or "root"
        folder = base if rel_dir in ("", "/") else base + "/" + rel_dir
        remote_dir = (remote_dir or "").strip().strip("/")
        provider_ids = (provider_ids or "").strip()
        with connect() as conn:
            existing = conn.execute(
                "SELECT status FROM queue_items WHERE path=?", (path,)).fetchone()
            if existing and existing["status"] in ("pending", "uploading"):
                # Already queued / in flight: never double-queue, and do not
                # silently re-route an item the user is already uploading.
                return False
            conn.execute(
                "INSERT INTO queue_items(path, filename, rel_path, folder, remote_dir, provider_ids, size) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET "
                " filename=excluded.filename, rel_path=excluded.rel_path, "
                " folder=excluded.folder, remote_dir=excluded.remote_dir, "
                " provider_ids=excluded.provider_ids, size=excluded.size, "
                " status=CASE WHEN queue_items.status IN ('completed','skipped') "
                "             THEN 'pending' ELSE queue_items.status END, "
                " error=CASE WHEN queue_items.status IN ('completed','skipped') "
                "            THEN NULL ELSE queue_items.error END, "
                " updated_at=datetime('now')",
                (path, os.path.basename(path), rel, folder, remote_dir, provider_ids, size))
            row = conn.execute(
                "SELECT status FROM queue_items WHERE path=?", (path,)).fetchone()
        # A file that reappears on disk after a completed/skipped upload is a
        # fresh download (e.g. after DELETE_AFTER_UPLOAD removed the old file):
        # re-queue it instead of silently ignoring it. Failed items stay failed
        # (the user retries them from the dashboard).
        if not row or row["status"] != "pending":
            return False
        logger.info("Queued: %s", path)
        return True
