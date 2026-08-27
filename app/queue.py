import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from . import config
from .database import connect
from .providers import get_provider_class
from .providers.s3 import S3MultipartError

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


class ProgressThrottler:
    """Decides whether a progress percentage is worth persisting.

    Upload progress callbacks fire per network chunk; writing every chunk to
    SQLite would hammer the database on large files. We only write when the
    value moved by at least `min_delta` points OR at least `interval` seconds
    have elapsed since the last write, so the dashboard still updates smoothly
    without the DB write storm.
    """

    def __init__(self, interval=2.0, min_delta=1.0):
        self.interval = interval
        self.min_delta = min_delta
        self._last_write = 0.0
        self._last_pct = -1.0

    def should_write(self, pct):
        now = time.monotonic()
        if (now - self._last_write) < self.interval and abs(pct - self._last_pct) < self.min_delta:
            return False
        self._last_write = now
        self._last_pct = pct
        return True


def _load_task_state(item_id, provider_id):
    """Load persisted resumable-upload state (e.g. S3 multipart upload id)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT state FROM upload_tasks WHERE item_id=? AND provider_id=?",
            (item_id, provider_id)).fetchone()
    if not row or not row["state"]:
        return None
    try:
        return json.loads(row["state"])
    except Exception:
        return None


def _dump_state(state):
    return json.dumps(state) if state else None


def compute_retry_delay(attempt):
    """Exponential backoff (seconds) for retry ``attempt``, with jitter and a
    hard cap so a down provider cannot stall the queue for hours.

    Pure function (testable): delay = min(cap, base * 2**(attempt-1)) + [0,jitter)
    """
    base = min(config.RETRY_BACKOFF_CAP,
               config.RETRY_BACKOFF * (2 ** max(0, attempt - 1)))
    if config.RETRY_JITTER > 0:
        base += random.uniform(0, config.RETRY_JITTER)
    return base


def _selected_providers(providers, provider_ids):
    """Filter the enabled providers down to the ones chosen for a watch path.

    An empty provider_ids means "use all enabled providers". Otherwise it is a
    comma-separated list of provider ids that restrict the upload target so each
    watch path (e.g. movies vs series) can go to its own provider."""
    ids = [int(x) for x in (provider_ids or "").split(",") if x.strip().isdigit()]
    if not ids:
        return providers
    wanted = set(ids)
    return [p for p in providers if p["id"] in wanted]


class QueueScheduler(threading.Thread):
    def __init__(self, notifier=None, concurrency=None):
        super().__init__(daemon=True, name="queue-scheduler")
        self.concurrency = concurrency or config.UPLOAD_CONCURRENCY
        self.paused = False
        self.running = True
        self._active = set()
        self._lock = threading.Lock()
        self.notifier = notifier
        # Cap of parallel upload workers per item, so an item routed to many
        # providers cannot spawn an unbounded number of threads.
        self.max_providers_per_item = config.MAX_PROVIDERS_PER_ITEM
        self.last_step_at = None
        self.started_total = 0
        self._last_prune = 0.0

    def run(self):
        logger.info("Queue scheduler started (concurrency=%d)", self.concurrency)
        while self.running:
            try:
                if not self.paused:
                    self._step()
                    self._recover_stale_uploading()
                    self._prune_old_completed()
            except Exception:
                logger.exception("scheduler step failed")
            time.sleep(3)

    def _prune_old_completed(self):
        """Housekeeping: delete completed items older than N days so the
        database cannot grow without bound on a 24/7 install."""
        if config.PRUNE_COMPLETED_DAYS <= 0:
            return
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        days = config.PRUNE_COMPLETED_DAYS
        with connect() as conn:
            cur = conn.execute(
                "DELETE FROM queue_items WHERE status='completed' "
                "AND completed_at IS NOT NULL "
                "AND completed_at < datetime('now', ?)", (f"-{days} days",))
        if cur.rowcount:
            logger.info("Pruned %d completed item(s) older than %d days",
                        cur.rowcount, days)

    def _recover_stale_uploading(self):
        """Watchdog: reset items stuck in 'uploading' whose worker thread is
        gone (e.g. the thread was killed without the finally block running).
        Without this, a single crash would consume a concurrency slot and
        stall the whole queue until restart."""
        with self._lock:
            active = set(self._active)
        with connect() as conn:
            rows = conn.execute(
                "SELECT id FROM queue_items WHERE status='uploading'").fetchall()
        for r in rows:
            if r["id"] not in active:
                logger.warning(
                    "Recovering stale 'uploading' item %s (worker thread gone); "
                    "resetting to pending", r["id"])
                with connect() as conn:
                    conn.execute(
                        "UPDATE queue_items SET status='pending', updated_at=datetime('now') "
                        "WHERE id=?", (r["id"],))

    def _step(self):
        self.last_step_at = time.time()
        # If no provider is enabled yet, do not start items: this avoids
        # repeatedly spawning short-lived worker threads for nothing.
        enabled = _enabled_providers()
        if not enabled:
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
            # Routing guard: if none of the providers selected for this item's
            # watch path is enabled, leave the item pending instead of starting
            # (and instantly re-pending) a worker thread every 3 seconds.
            if not _selected_providers(enabled, row["provider_ids"]):
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
        self.started_total += 1
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
            providers = _selected_providers(_enabled_providers(), item["provider_ids"])
            if not providers:
                with connect() as conn:
                    conn.execute(
                        "UPDATE queue_items SET status='pending', updated_at=datetime('now') "
                        "WHERE id=?", (item_id,))
                logger.warning(
                    "No enabled provider for item %s (path providers: %s); stays pending",
                    item["filename"], item["provider_ids"] or "all")
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
            # Optional per-watch-path remote folder: prefix the path so each
            # source (e.g. Radarr vs Sonarr) lands in its own cloud folder.
            # Only applied for providers that use real directory paths; the
            # FileHost provider ignores it (it only uses the file basename).
            remote_dir = (item["remote_dir"] or "").strip().strip("/")
            if remote_dir:
                remote = remote_dir + "/" + remote
            # Upload to the selected providers in parallel, but never spawn
            # more than a sane number of threads per item.
            workers = min(len(providers), self.max_providers_per_item)
            with ThreadPoolExecutor(max_workers=workers,
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
            if not tasks:
                # All upload tasks disappeared while the workers were running
                # (e.g. the user deleted every provider mid-upload). We have no
                # proof the file reached anywhere, so never mark it completed
                # and never delete the local file.
                self._mark_failed(
                    item_id, "All upload tasks were removed while uploading")
                return
            # Any task that is not 'completed' (e.g. 'pending' because its worker
            # crashed, or 'failed') means the file did NOT finish uploading, so
            # we must NOT delete it.
            not_done = [t for t in tasks if t["status"] != "completed"]
            if not_done:
                errs = "; ".join(
                    f"{t['provider_name']}: {t['error'] or ('crashed' if t['status'] == 'pending' else 'failed')}"
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
                        f"Uploaded successfully: {item['filename']}"
                        f"{' | local file deleted' if deleted else ''}")
        except Exception as e:
            # Never leave the item stuck in 'uploading' (which would consume a
            # concurrency slot forever). Mark it failed so the error is visible
            # and the slot is freed; the user can retry from the dashboard.
            logger.exception("Unexpected error processing item %s", item_id)
            try:
                self._mark_failed(item_id, f"Internal error: {str(e)[:300]}")
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

        try:
            inst = cls(prov["config"])
        except Exception as e:
            with connect() as conn:
                conn.execute(
                    "UPDATE upload_tasks SET status='failed', error=?, "
                    "updated_at=datetime('now') WHERE item_id=? AND provider_id=?",
                    (f"Provider init failed: {str(e)[:300]}", item_id, provider_id))
            return
        max_attempts = max(1, config.RETRY_MAX)

        # Throttle progress writes: large files would otherwise generate one
        # SQLite UPDATE per network chunk (hundreds of thousands), hammering
        # the database. We persist at most every ~2s and only when the
        # percentage moved by at least 1 point.
        throttler = ProgressThrottler(interval=2.0, min_delta=1.0)

        def progress(fraction):
            pct = round(min(1.0, max(0.0, fraction)) * 100, 1)
            if not throttler.should_write(pct):
                return
            try:
                with connect() as conn:
                    conn.execute(
                        "UPDATE upload_tasks SET progress=?, updated_at=datetime('now') "
                        "WHERE item_id=? AND provider_id=?", (pct, item_id, provider_id))
            except Exception:
                pass

        # Resumable upload state (S3 multipart upload id + parts): persisted in
        # upload_tasks.state so a retry — or a container restart — continues
        # from the last uploaded part instead of re-uploading the whole file.
        resume_state = _load_task_state(item_id, provider_id)
        state_holder = {"state": resume_state}
        attempts = {"n": 0}

        def _attempt_upload():
            attempts["n"] += 1
            try:
                inst.upload(item["path"], remote, progress,
                            resume_state=state_holder["state"])
            except S3MultipartError as e:
                # Partial progress: remember it for the next attempt.
                state_holder["state"] = e.state or state_holder["state"]
                raise

        def _persist_failed(retry_state):
            err = str(retry_state.outcome.exception())[:500]
            logger.warning(
                "Upload failed for %s (provider %s), attempt %d/%d: %s",
                item["filename"], provider_name, attempts["n"], max_attempts, err)
            with connect() as conn:
                conn.execute(
                    "UPDATE upload_tasks SET status='failed', attempts=?, error=?, state=?, "
                    "updated_at=datetime('now') WHERE item_id=? AND provider_id=?",
                    (attempts["n"], err, _dump_state(state_holder["state"]),
                     item_id, provider_id))

        # tenacity: exponential backoff (identical formula to compute_retry_delay)
        # plus jitter, capped, with the failure state persisted before each sleep.
        retrier = Retrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=config.RETRY_BACKOFF,
                                  max=config.RETRY_BACKOFF_CAP)
                 + wait_random(0, config.RETRY_JITTER),
            retry=retry_if_exception_type(Exception),
            before_sleep=_persist_failed,
            reraise=True,
        )

        try:
            retrier(_attempt_upload)
        except Exception as e:
            err = str(e)[:500]
            logger.error("Gave up on %s (provider %s): %s",
                         item["filename"], provider_name, err)
            with connect() as conn:
                conn.execute(
                    "UPDATE upload_tasks SET status='failed', attempts=?, error=?, state=?, "
                    "updated_at=datetime('now') WHERE item_id=? AND provider_id=?",
                    (attempts["n"], err, _dump_state(state_holder["state"]),
                     item_id, provider_id))
            return

        # Success: the provider confirmed the upload; resume state is cleared.
        with connect() as conn:
            conn.execute(
                "UPDATE upload_tasks SET status='completed', progress=100, error=NULL, "
                "attempts=?, state=NULL, updated_at=datetime('now') "
                "WHERE item_id=? AND provider_id=?",
                (attempts["n"], item_id, provider_id))
        logger.info("Uploaded %s -> %s (provider %s)",
                    item["filename"], remote, provider_name)

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
            self.notifier.notify(f"Upload FAILED: {name}\n{error[:2000]}")
