import logging
import os
import threading
import time

from . import config
from .database import connect

logger = logging.getLogger("uploader.watcher")


def is_incomplete_name(name):
    low = name.lower()
    for suffix in config.INCOMPLETE_SUFFIXES:
        if low.endswith(suffix):
            return True
    for marker in config.INCOMPLETE_DIR_MARKERS:
        if marker in low:
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

    def run(self):
        logger.info("Watcher started (scan every %ds)", config.SCAN_INTERVAL)
        while self.running:
            try:
                self.scan_once()
            except Exception:
                logger.exception("scan failed")
            time.sleep(config.SCAN_INTERVAL)

    def scan_once(self):
        result = {"paths": 0, "files": 0, "queued": 0}
        with connect() as conn:
            roots = [dict(r) for r in conn.execute(
                "SELECT path, remote_dir FROM watch_paths WHERE enabled=1")]
        seen = set()
        for root_rec in roots:
            root = os.path.abspath(root_rec["path"])
            remote_dir = root_rec["remote_dir"] or ""
            if not os.path.isdir(root):
                if root not in self._warned:
                    logger.warning(
                        "Watch path does not exist inside the container: %s. "
                        "Check the volume mount in docker-compose.yml.", root)
                    self._warned.add(root)
                    if self.notifier:
                        self.notifier.notify(
                            "Watch path not found in container: %s" % root)
                continue
            result["paths"] += 1
            for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
                dirnames[:] = sorted(
                    d for d in dirnames if not is_incomplete_name(d))
                for name in sorted(filenames):
                    if name.startswith("."):
                        continue
                    if is_incomplete_name(name):
                        continue
                    full = os.path.join(dirpath, name)
                    if not os.path.isfile(full):
                        continue
                    seen.add(full)
                    result["files"] += 1
                    if self._check(full, root, remote_dir):
                        result["queued"] += 1
        with self._stable_lock:
            stale = [p for p in self._stable if p not in seen]
            for p in stale:
                self._stable.pop(p, None)
        return result

    def _check(self, path, root, remote_dir=""):
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
                return self._enqueue(path, root, st.st_size, remote_dir)
            if age >= config.STABLE_SECONDS:
                return self._enqueue(path, root, st.st_size, remote_dir)
            self._stable[path] = (st.st_size, st.st_mtime, now)
        return False

    def _enqueue(self, path, root, size, remote_dir=""):
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            rel = os.path.basename(path)
        rel = rel.replace(os.sep, "/")
        rel_dir = os.path.dirname(rel)
        base = os.path.basename(root.rstrip("/")) or "root"
        folder = base if rel_dir in ("", "/") else base + "/" + rel_dir
        remote_dir = (remote_dir or "").strip().strip("/")
        with connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO queue_items(path, filename, rel_path, folder, remote_dir, size) "
                "VALUES(?,?,?,?,?,?)",
                (path, os.path.basename(path), rel, folder, remote_dir, size))
            if cur.rowcount == 0:
                return False
        logger.info("Queued: %s", path)
        return True
