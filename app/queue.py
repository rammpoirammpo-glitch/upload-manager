import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config
from .database import connect
from .providers import get_provider_class

logger = logging.getLogger("uploader.queue")


def _enabled_providers():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM providers WHERE enabled=1 ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["config"] = json.loads(d.get("config") or "{}")
        except Exception:
            d["config"] = {}
        out.append(d)
    return out


class QueueScheduler(threading.Thread):
    def __init__(self, notifier=None, concurrency=None):
        super().__init__(daemon=True, name="queue-scheduler")
        self.concurrency = concurrency or config.UPLOAD_CONCURRENCY
        self.paused = False
        self.running = True
        self._active = set()
        self._lock = threading.Lock()
        self.notifier = notifier

    def run(self):
        logger.info("Queue scheduler started (concurrency=%d)", self.concurrency)
        while self.running:
            try:
                if not self.paused:
                    self._step()
            except Exception:
                logger.exception("scheduler step failed")
            time.sleep(3)

    def _step(self):
        # If no provider is enabled yet, do not start items: this avoids
        # repeatedly spawning short-lived worker threads for nothing.
        if not _enabled_providers():
            return
        with connect() as conn:
            active = [r["id"] for r in conn.execute(
                "SELECT id FROM queue_items WHERE status='uploading'")]
        free = self.concurrency - len(active)
        if free <= 0:
            return
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM queue_items WHERE status='pending' ORDER BY id").fetchall()
        started = 0
        for row in rows:
            if started >= free:
                break
            with self._lock:
                if row["id"] in self._active:
                    continue
            if self._blocked(row):
                continue
            if not row["size"] or row["size"] <= 0:
                self._mark_failed(row["id"], "File size is zero or unknown")
                continue
            self._start_item(row)
            started += 1

    def _blocked(self, row):
        # Only in-flight (pending/uploading) earlier items block a group.
        # A permanently failed item must NOT stall the rest of the group.
        with connect() as conn:
            r = conn.execute(
                "SELECT COUNT(*) AS c FROM queue_items "
                "WHERE folder=? AND status IN ('pending','uploading') AND id < ?",
                (row["folder"], row["id"])).fetchone()
        return (r["c"] or 0) > 0

    def _start_item(self, row):
        item_id = row["id"]
        with self._lock:
            self._active.add(item_id)
        with connect() as conn:
            conn.execute(
                "UPDATE queue_items SET status='uploading', updated_at=datetime('now'), "
                "started_at=datetime('now') WHERE id=?", (item_id,))
        t = threading.Thread(target=self._process_item, args=(item_id,),
                             daemon=True, name=f"item-{item_id}")
        t.start()

    def _process_item(self, item_id):
        try:
            with connect() as conn:
                item = conn.execute(
                    "SELECT * FROM queue_items WHERE id=?", (item_id,)).fetchone()
            if not item:
                return
            providers = _enabled_providers()
            if not providers:
                with connect() as conn:
                    conn.execute(
                        "UPDATE queue_items SET status='pending', updated_at=datetime('now') "
                        "WHERE id=?", (item_id,))
                logger.warning(
                    "No enabled provider configured yet; item %s stays pending",
                    item["filename"])
                return
            with connect() as conn:
                for p in providers:
                    conn.execute(
                        "INSERT OR IGNORE INTO upload_tasks(item_id, provider_id) "
                        "VALUES(?,?)", (item_id, p["id"]))
                conn.execute(
                    "UPDATE upload_tasks SET status='pending', progress=0, error=NULL "
                    "WHERE item_id=? AND status IN ('pending','failed')", (item_id,))
            remote = item["rel_path"] or os.path.basename(item["path"])
            remote = remote.replace(os.sep, "/")
            with ThreadPoolExecutor(max_workers=len(providers),
                                    thread_name_prefix="upload") as ex:
                futures = [ex.submit(self._upload_to_provider, item_id, p, remote)
                           for p in providers]
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception:
                        logger.exception("provider worker crashed")

            with connect() as conn:
                tasks = conn.execute(
                    "SELECT t.*, p.name AS provider_name FROM upload_tasks t "
                    "JOIN providers p ON p.id=t.provider_id WHERE t.item_id=?",
                    (item_id,)).fetchall()
            # Any task that is not 'completed' (e.g. 'pending' because its worker
            # crashed, or 'failed') means the file did NOT finish uploading, so
            # we must NOT delete it.
            not_done = [t for t in tasks if t["status"] != "completed"]
            if not_done:
                errs = "; ".join(
                    "%s: %s" % (t["provider_name"],
                                t["error"] or ("crashed" if t["status"] == "pending" else "failed"))
                    for t in not_done)
                self._mark_failed(item_id, errs)
            else:
                with connect() as conn:
                    conn.execute(
                        "UPDATE queue_items SET status='completed', error=NULL, "
                        "updated_at=datetime('now'), completed_at=datetime('now') "
                        "WHERE id=?", (item_id,))
                logger.info("Item %s completed", item["filename"])
                if config.DELETE_AFTER_UPLOAD:
                    deleted = self._delete_local(item)
                else:
                    deleted = False
                if self.notifier:
                    self.notifier.notify(
                        "Uploaded successfully: %s%s" % (
                            item["filename"],
                            " | local file deleted" if deleted else ""))
        except Exception as e:
            # Never leave the item stuck in 'uploading' (which would consume a
            # concurrency slot forever). Mark it failed so the error is visible
            # and the slot is freed; the user can retry from the dashboard.
            logger.exception("Unexpected error processing item %s", item_id)
            try:
                self._mark_failed(item_id, "Internal error: %s" % str(e)[:300])
            except Exception:
                logger.exception("Could not mark item %s failed", item_id)
        finally:
            with self._lock:
                self._active.discard(item_id)

    def _upload_to_provider(self, item_id, prov, remote):
        provider_id = prov["id"]
        provider_name = prov["name"]
        cls = get_provider_class(prov["type"])
        if cls is None:
            with connect() as conn:
                conn.execute(
                    "UPDATE upload_tasks SET status='failed', "
                    "error='Unknown provider type', updated_at=datetime('now') "
                    "WHERE item_id=? AND provider_id=?",
                    (item_id, provider_id))
            return
        with connect() as conn:
            item = conn.execute(
                "SELECT * FROM queue_items WHERE id=?", (item_id,)).fetchone()
        total = item["size"]
        if total <= 0:
            with connect() as conn:
                conn.execute(
                    "UPDATE upload_tasks SET status='failed', error='Empty file', "
                    "updated_at=datetime('now') WHERE item_id=? AND provider_id=?",
                    (item_id, provider_id))
            return
        if not os.path.exists(item["path"]):
            with connect() as conn:
                conn.execute(
                    "UPDATE upload_tasks SET status='failed', "
                    "error='Source file not found', updated_at=datetime('now') "
                    "WHERE item_id=? AND provider_id=?",
                    (item_id, provider_id))
            return

        inst = cls(prov["config"])
        max_attempts = max(1, config.RETRY_MAX)

        def progress(fraction):
            pct = round(min(1.0, max(0.0, fraction)) * 100, 1)
            try:
                with connect() as conn:
                    conn.execute(
                        "UPDATE upload_tasks SET progress=?, updated_at=datetime('now') "
                        "WHERE item_id=? AND provider_id=?", (pct, item_id, provider_id))
            except Exception:
                pass

        for attempt in range(1, max_attempts + 1):
            try:
                inst.upload(item["path"], remote, progress)
                with connect() as conn:
                    conn.execute(
                        "UPDATE upload_tasks SET status='completed', progress=100, "
                        "error=NULL, attempts=?, updated_at=datetime('now') "
                        "WHERE item_id=? AND provider_id=?",
                        (attempt, item_id, provider_id))
                logger.info("Uploaded %s -> %s (provider %s)",
                            item["filename"], remote, provider_name)
                return
            except Exception as e:
                err = str(e)[:500]
                logger.warning("Upload failed for %s (provider %s): %s",
                               item["filename"], provider_name, err)
                with connect() as conn:
                    conn.execute(
                        "UPDATE upload_tasks SET status='failed', attempts=?, error=?, "
                        "updated_at=datetime('now') WHERE item_id=? AND provider_id=?",
                        (attempt, err, item_id, provider_id))
                if attempt < max_attempts:
                    wait = config.RETRY_BACKOFF * (2 ** (attempt - 1))
                    logger.info("Retrying %s in %ds (attempt %d/%d)",
                                item["filename"], wait, attempt + 1, max_attempts)
                    time.sleep(wait)
        logger.error("Gave up on %s (provider %s)", item["filename"], provider_name)

    def _delete_local(self, item):
        """Delete the source file after a successful upload to save disk space.
        Also removes the now-empty parent directory if it is not a watch root.
        Returns True if the file was deleted."""
        path = item["path"]
        if not path or not os.path.exists(path):
            return False
        try:
            os.remove(path)
            logger.info("Deleted local file after upload: %s", path)
        except OSError as e:
            logger.warning("Could not delete local file %s: %s", path, e)
            return False

        roots = set()
        try:
            with connect() as conn:
                for r in conn.execute("SELECT path FROM watch_paths"):
                    roots.add(os.path.abspath(r["path"]))
        except Exception:
            pass
        d = os.path.dirname(path)
        while d and os.path.abspath(d) not in roots and os.path.abspath(d) != os.path.abspath(os.sep):
            try:
                os.rmdir(d)
            except OSError:
                break
            d = os.path.dirname(d)
        return True

    def _mark_failed(self, item_id, error):
        with connect() as conn:
            row = conn.execute(
                "SELECT filename FROM queue_items WHERE id=?", (item_id,)).fetchone()
            conn.execute(
                "UPDATE queue_items SET status='failed', error=?, "
                "updated_at=datetime('now'), completed_at=datetime('now') "
                "WHERE id=?", (error[:2000], item_id))
        if self.notifier:
            name = row["filename"] if row else f"item #{item_id}"
            self.notifier.notify(
                "Upload FAILED: %s\n%s" % (name, error[:2000]))
