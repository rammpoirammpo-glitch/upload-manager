"""Tests for the upload-manager app.

Run with:  python -m unittest discover -s tests -v   (from the repo root)
Requires the runtime dependencies installed (pip install -r requirements.txt).

The DATA_DIR env var is pointed at a temp folder BEFORE importing the app, so
config.py picks up an isolated database for every test.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="uploadmgr-test-")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["DELETE_AFTER_UPLOAD"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config  # noqa: E402
from app.api import _clean_provider_ids  # noqa: E402
from app.database import connect, init_db  # noqa: E402
from app.notifier import Notifier  # noqa: E402
from app.providers.filehost import FileHostProvider  # noqa: E402
from app.queue import (  # noqa: E402
    ProgressThrottler,
    QueueScheduler,
    _enabled_providers,
    _selected_providers,
    compute_retry_delay,
)
from app.watcher import Watcher, is_incomplete_name  # noqa: E402


def _provider_row(pid, name="p"):
    return {"id": pid, "name": name, "type": "local", "enabled": 1, "config": {}}


class IncompleteNameTests(unittest.TestCase):
    def test_file_suffixes(self):
        for name in ("movie.mkv.part", "movie.mkv.!qb", "movie.mkv.partial",
                     "movie.mkv.incomplete", "movie.mkv.crdownload",
                     "movie.mkv.aria2", "movie.mkv.tmp"):
            self.assertTrue(is_incomplete_name(name, is_dir=False), name)
            self.assertTrue(is_incomplete_name(name, is_dir=True), name)

    def test_legit_filenames_never_skipped(self):
        # "temp" / "partial" appear inside legit names: these must NOT be
        # treated as incomplete (previous bug skipped them forever).
        for name in ("Temperature.2023.1080p.mkv", "The.Partial.Mind.avi",
                     "temporary.office.s01e01.mp4", "part2.mkv",
                     "Movie.Name.mkv"):
            self.assertFalse(is_incomplete_name(name, is_dir=False), name)

    def test_dir_markers_only_for_dirs(self):
        # Directory-level markers (qBittorrent style) skip dirs...
        self.assertTrue(is_incomplete_name("Movie.Name.!qb", is_dir=True))
        self.assertTrue(is_incomplete_name("partial", is_dir=True))
        self.assertTrue(is_incomplete_name("temp", is_dir=True))
        # ...but a normal folder named "Temperature" (marker inside the word)
        # must NOT be skipped, while a folder named exactly "partial"/"temp"
        # still is (safer to skip a possible incomplete download than to
        # upload a half-written folder).
        self.assertFalse(is_incomplete_name("Temperature", is_dir=True))
        self.assertFalse(is_incomplete_name("Partial.Mind", is_dir=True))
        self.assertTrue(is_incomplete_name("Partial", is_dir=True))


class RoutingTests(unittest.TestCase):
    def test_empty_provider_ids_uses_all(self):
        provs = [_provider_row(1), _provider_row(2), _provider_row(3)]
        self.assertEqual(_selected_providers(provs, ""), provs)
        self.assertEqual(_selected_providers(provs, None), provs)

    def test_subset_selected(self):
        provs = [_provider_row(1), _provider_row(2), _provider_row(3)]
        sel = _selected_providers(provs, "1,3")
        self.assertEqual([p["id"] for p in sel], [1, 3])

    def test_unknown_ids_ignored(self):
        provs = [_provider_row(1), _provider_row(2)]
        sel = _selected_providers(provs, "1,999,banana")
        self.assertEqual([p["id"] for p in sel], [1])

    def test_clean_provider_ids_filters_deleted(self):
        # A watch path must never route to a provider that no longer exists.
        self.assertEqual(_clean_provider_ids("1,3,99", valid_ids={1, 3}), "1,3")
        self.assertEqual(_clean_provider_ids("", valid_ids={1}), "")
        self.assertEqual(_clean_provider_ids("99", valid_ids={1}), "")
        self.assertEqual(_clean_provider_ids("2", valid_ids={1, 2}), "2")


class FileHostStatusTests(unittest.TestCase):
    def test_status_variants(self):
        ok = FileHostProvider._status_ok
        self.assertTrue(ok({"status": 200}))
        self.assertTrue(ok({"status": "200"}))
        self.assertTrue(ok({}))  # missing status == success (Xvids clones)
        self.assertTrue(ok({"status": True}))
        self.assertFalse(ok({"status": 400}))
        self.assertFalse(ok({"status": "403"}))
        self.assertFalse(ok({"status": None}))
        self.assertFalse(ok({"status": 0}))


class ProgressThrottlerTests(unittest.TestCase):
    def test_first_write_always(self):
        t = ProgressThrottler(interval=60.0, min_delta=1.0)
        self.assertTrue(t.should_write(5.0))

    def test_small_delta_within_interval_skipped(self):
        t = ProgressThrottler(interval=60.0, min_delta=1.0)
        t.should_write(10.0)
        self.assertFalse(t.should_write(10.4))  # < 1 point delta
        self.assertTrue(t.should_write(11.0))   # >= 1 point delta

    def test_large_delta_within_interval_written(self):
        t = ProgressThrottler(interval=60.0, min_delta=1.0)
        t.should_write(10.0)
        self.assertTrue(t.should_write(30.0))

    def test_time_interval_forces_write(self):
        t = ProgressThrottler(interval=0.0, min_delta=100.0)
        t.should_write(1.0)
        self.assertTrue(t.should_write(1.01))


class BackoffTests(unittest.TestCase):
    """Exponential backoff must grow, stay inside the jitter band, and never
    exceed the hard cap (so a down provider cannot stall the queue forever)."""

    def test_delay_within_bounds(self):
        for attempt in (1, 2, 3, 4, 5):
            d = compute_retry_delay(attempt)
            base = min(config.RETRY_BACKOFF_CAP,
                       config.RETRY_BACKOFF * (2 ** (attempt - 1)))
            self.assertGreaterEqual(d, base, f"attempt {attempt}")
            self.assertLess(d, base + config.RETRY_JITTER + 1e-6,
                            f"attempt {attempt}")

    def test_delay_capped(self):
        # attempt=10 -> 30 * 2**9 = 15360s, far above the 300s cap
        d = compute_retry_delay(10)
        self.assertLessEqual(d, config.RETRY_BACKOFF_CAP + config.RETRY_JITTER)

    def test_delay_monotonic(self):
        samples = [compute_retry_delay(a) for a in (1, 2, 3, 4, 5)]
        self.assertEqual(samples, sorted(samples))


class NotifierTests(unittest.TestCase):
    def test_rate_limit_blocks_immediate_duplicate(self):
        n = Notifier()
        self.assertTrue(n._allow("key1"))
        self.assertFalse(n._allow("key1"))
        self.assertTrue(n._allow("key2"))

    def test_cooldown_key_normalizes(self):
        n = Notifier()
        self.assertEqual(n._cooldown_key("  hello world  "), "hello world")

    def test_notify_noop_without_creds(self):
        n = Notifier()
        n.notify("hello")
        self.assertEqual(n._queue.qsize(), 0)


class QueuePipelineTests(unittest.TestCase):
    """Full pipeline: scan -> queue -> upload -> complete, with a LocalProvider
    (no network needed). Verifies routing and re-queue behavior."""

    @classmethod
    def setUpClass(cls):
        init_db()
        cls.watch = tempfile.mkdtemp(prefix="watch-")
        cls.target = tempfile.mkdtemp(prefix="target-")
        with connect() as conn:
            conn.execute("DELETE FROM queue_items")
            conn.execute("DELETE FROM upload_tasks")
            conn.execute("DELETE FROM providers")
            conn.execute("DELETE FROM watch_paths")
            conn.execute(
                "INSERT INTO providers(name,type,config,enabled) VALUES(?,?,?,?)",
                ("local-test", "local",
                 '{"target_dir": "%s"}' % cls.target, 1))  # noqa: UP031
            conn.execute(
                "INSERT INTO watch_paths(path, enabled, remote_dir, provider_ids) "
                "VALUES(?,?,?,?)", (cls.watch, 1, "", ""))
        cls.watcher = Watcher(notifier=None)
        cls.scheduler = QueueScheduler(notifier=None, concurrency=2)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    def _write_movie(self, name="Movie.2024.1080p.mkv", size=1024 * 1024):
        p = os.path.join(self.watch, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        # mtime must be old enough to count as stable
        os.utime(p, (1, 1))
        return p

    def test_full_pipeline_upload_completes_and_deletes(self):
        src = self._write_movie()
        res = self.watcher.scan_once()
        self.assertEqual(res["queued"], 1)
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM queue_items WHERE path=?", (src,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending")
        # Routing: item must be assigned to the enabled provider
        self.assertEqual(row["provider_ids"], "")
        item_id = row["id"]

        self.scheduler._process_item(item_id)

        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM queue_items WHERE id=?", (item_id,)).fetchone()
            tasks = conn.execute(
                "SELECT * FROM upload_tasks WHERE item_id=?", (item_id,)).fetchall()
        self.assertEqual(row["status"], "completed", row["error"])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "completed")
        # Local copy exists in the provider target. AUTO_REMOTE_FOLDER adds a
        # cloud folder named after the watch path's source folder.
        cloud_folder = os.path.basename(self.watch.rstrip(os.sep))
        dest = os.path.join(self.target, cloud_folder, "Movie.2024.1080p.mkv")
        self.assertTrue(os.path.exists(dest), f"expected {dest}")
        self.assertEqual(os.path.getsize(dest), 1024 * 1024)
        # DELETE_AFTER_UPLOAD=true removed the source
        self.assertFalse(os.path.exists(src))

    def test_reappearing_file_is_requeued(self):
        # Simulate a re-download after DELETE_AFTER_UPLOAD removed the file:
        # the file appears again on disk -> must be queued as pending again,
        # even though a completed queue row already exists for that path.
        src = self._write_movie("ReDownload.mkv")
        res1 = self.watcher.scan_once()
        self.assertEqual(res1["queued"], 1)
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM queue_items WHERE path=?", (src,)).fetchone()
        item_id = row["id"]
        self.scheduler._process_item(item_id)
        with connect() as conn:
            status = conn.execute(
                "SELECT status FROM queue_items WHERE id=?", (item_id,)).fetchone()
        self.assertEqual(status["status"], "completed")

        self._write_movie("ReDownload.mkv")
        res2 = self.watcher.scan_once()
        self.assertEqual(res2["queued"], 1, "reappearing file must be re-queued")
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM queue_items WHERE path=?", (src,)).fetchone()
        self.assertEqual(row["status"], "pending")

    def test_stale_uploading_recovery(self):
        with connect() as conn:
            conn.execute("INSERT INTO queue_items(path, filename, status) "
                         "VALUES(?,?,?)", ("/nonexistent/x.mkv", "x.mkv", "uploading"))
            fake = conn.execute(
                "SELECT id FROM queue_items WHERE path='/nonexistent/x.mkv'").fetchone()["id"]
        # Item is 'uploading' in the DB but its worker thread is gone (not in
        # self._active) -> the watchdog must reset it so it does not block a slot.
        self.scheduler._recover_stale_uploading()
        with connect() as conn:
            row = conn.execute(
                "SELECT status FROM queue_items WHERE id=?", (fake,)).fetchone()
        self.assertEqual(row["status"], "pending")

    def test_routing_isolation_at_scale(self):
        """Radarr movies -> provider A, Sonarr series -> provider B, with a
        queue of 80 items: every item must route to exactly one provider and
        land only in its target, with zero cross-routing."""
        movies_dir = tempfile.mkdtemp(prefix="movies-")
        series_dir = tempfile.mkdtemp(prefix="series-")
        ta = tempfile.mkdtemp(prefix="hostA-")
        tb = tempfile.mkdtemp(prefix="hostB-")
        with connect() as conn:
            conn.execute(
                "INSERT INTO providers(name,type,config,enabled) VALUES(?,?,?,?)",
                ("scaleA", "local", json.dumps({"target_dir": ta}), 1))
            a_id = conn.execute(
                "SELECT id FROM providers WHERE name='scaleA'").fetchone()["id"]
            conn.execute(
                "INSERT INTO providers(name,type,config,enabled) VALUES(?,?,?,?)",
                ("scaleB", "local", json.dumps({"target_dir": tb}), 1))
            b_id = conn.execute(
                "SELECT id FROM providers WHERE name='scaleB'").fetchone()["id"]
            conn.execute(
                "INSERT INTO watch_paths(path, enabled, remote_dir, provider_ids) "
                "VALUES(?,?,?,?)", (movies_dir, 1, "", str(a_id)))
            conn.execute(
                "INSERT INTO watch_paths(path, enabled, remote_dir, provider_ids) "
                "VALUES(?,?,?,?)", (series_dir, 1, "", str(b_id)))

        def _write(d, name):
            p = os.path.join(d, name)
            with open(p, "wb") as f:
                f.write(b"x" * 1024)
            os.utime(p, (1, 1))
            return p

        for i in range(40):
            _write(movies_dir, f"Movie.{i:03d}.mkv")
            _write(series_dir, f"Show.S{i:02d}E01.mkv")

        res = self.watcher.scan_once()
        self.assertEqual(res["queued"], 80, res)
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM queue_items WHERE path LIKE ? OR path LIKE ? "
                "ORDER BY id", (movies_dir + "%", series_dir + "%")).fetchall()
        self.assertEqual(len(rows), 80)

        # Routing exactness for every single item, regardless of queue size
        enabled = _enabled_providers()
        for r in rows:
            want = a_id if r["path"].startswith(movies_dir) else b_id
            got = [p["id"] for p in _selected_providers(enabled, r["provider_ids"])]
            self.assertEqual(got, [want], r["path"])

        # Process all 80 items: everything must complete into the right target
        for r in rows:
            self.scheduler._process_item(r["id"])
        with connect() as conn:
            bad = conn.execute(
                "SELECT COUNT(*) AS c FROM queue_items "
                "WHERE (path LIKE ? OR path LIKE ?) AND status!='completed'",
                (movies_dir + "%", series_dir + "%")).fetchone()["c"]
        self.assertEqual(bad, 0)
        cf_m = os.path.basename(movies_dir.rstrip(os.sep))
        cf_s = os.path.basename(series_dir.rstrip(os.sep))
        for i in range(40):
            self.assertTrue(os.path.exists(
                os.path.join(ta, cf_m, f"Movie.{i:03d}.mkv")))
            self.assertFalse(os.path.exists(
                os.path.join(tb, cf_m, f"Movie.{i:03d}.mkv")))
            self.assertTrue(os.path.exists(
                os.path.join(tb, cf_s, f"Show.S{i:02d}E01.mkv")))
            self.assertFalse(os.path.exists(
                os.path.join(ta, cf_s, f"Show.S{i:02d}E01.mkv")))

    def test_step_skips_items_without_enabled_provider(self):
        """The scheduler must NOT start (and instantly re-pend) an item whose
        routing points at a provider that does not exist / is not enabled: it
        stays pending and consumes no concurrency slot (no busy loop)."""
        with connect() as conn:
            conn.execute("DELETE FROM queue_items")
            conn.execute(
                "INSERT INTO queue_items(path, filename, provider_ids, size, status) "
                "VALUES(?,?,?,?,?)", ("/x/unroutable.mkv", "unroutable.mkv",
                                       "999999", 1024, "pending"))
            unroutable_id = conn.execute(
                "SELECT id FROM queue_items WHERE path='/x/unroutable.mkv'").fetchone()["id"]
        before = self.scheduler.started_total
        self.scheduler._step()
        with connect() as conn:
            st = conn.execute(
                "SELECT status FROM queue_items WHERE id=?", (unroutable_id,)).fetchone()["status"]
            uploading = conn.execute(
                "SELECT COUNT(*) AS c FROM queue_items WHERE status='uploading'").fetchone()["c"]
        self.assertEqual(self.scheduler.started_total, before)
        self.assertEqual(st, "pending")
        self.assertEqual(uploading, 0)
        with connect() as conn:
            conn.execute("DELETE FROM queue_items WHERE id=?", (unroutable_id,))

    def test_prune_old_completed(self):
        with connect() as conn:
            conn.execute("DELETE FROM queue_items")
            conn.execute(
                "INSERT INTO queue_items(path, filename, status, completed_at) "
                "VALUES(?,?,?,datetime('now','-40 days'))",
                ("/x/old.mkv", "old.mkv", "completed"))
            conn.execute(
                "INSERT INTO queue_items(path, filename, status, completed_at) "
                "VALUES(?,?,?,datetime('now'))",
                ("/x/new.mkv", "new.mkv", "completed"))
            conn.execute(
                "INSERT INTO queue_items(path, filename, status) VALUES(?,?,?)",
                ("/x/pend.mkv", "pend.mkv", "pending"))
        self.scheduler._prune_old_completed()
        with connect() as conn:
            paths = [r["path"] for r in conn.execute("SELECT path FROM queue_items")]
        self.assertNotIn("/x/old.mkv", paths)
        self.assertIn("/x/new.mkv", paths)
        self.assertIn("/x/pend.mkv", paths)


class SkipRetryTests(unittest.TestCase):
    """skip/retry semantics at the DB level (same SQL the API uses)."""

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        with connect() as conn:
            conn.execute("DELETE FROM queue_items")
            conn.execute("DELETE FROM upload_tasks")
            cur = conn.execute(
                "INSERT INTO queue_items(path, filename, status) VALUES(?,?,?)",
                ("/nonexistent/skip.mkv", "skip.mkv", "failed"))
            self.item_id = cur.lastrowid
            for i, status in enumerate(("failed", "pending", "completed"), start=1):
                conn.execute(
                    "INSERT INTO upload_tasks(item_id, provider_id, status) VALUES(?,?,?)",
                    (self.item_id, i, status))

    def test_skip_marks_failed_and_pending_tasks_skipped(self):
        # Same statements as api.skip_item
        with connect() as conn:
            conn.execute(
                "UPDATE upload_tasks SET status='skipped', error='Skipped by user' "
                "WHERE item_id=? AND status IN ('failed','pending')", (self.item_id,))
            conn.execute(
                "UPDATE queue_items SET status='skipped', error=NULL, "
                "updated_at=datetime('now') WHERE id=?", (self.item_id,))
        with connect() as conn:
            tasks = {t["status"] for t in conn.execute(
                "SELECT status FROM upload_tasks WHERE item_id=?", (self.item_id,))}
        self.assertEqual(tasks, {"skipped", "completed"})

    def test_retry_resets_everything_to_pending(self):
        with connect() as conn:
            conn.execute(
                "UPDATE upload_tasks SET status='pending', progress=0, error=NULL, "
                "attempts=0 WHERE item_id=?", (self.item_id,))
            conn.execute(
                "UPDATE queue_items SET status='pending', error=NULL, attempts=0, "
                "updated_at=datetime('now') WHERE id=?", (self.item_id,))
        with connect() as conn:
            item = conn.execute(
                "SELECT status FROM queue_items WHERE id=?", (self.item_id,)).fetchone()
            tasks = {t["status"] for t in conn.execute(
                "SELECT status FROM upload_tasks WHERE item_id=?", (self.item_id,))}
        self.assertEqual(item["status"], "pending")
        self.assertEqual(tasks, {"pending"})


# --------------------------------------------------------------------------
# API-level integration tests (routing accuracy). Requires httpx (dev-only
# dependency) for TestClient; skipped when it is missing.
# --------------------------------------------------------------------------

try:
    from fastapi.testclient import TestClient

    from app.main import app
    _HAVE_HTTPX = True
except ImportError:  # httpx not installed -> skip these tests
    _HAVE_HTTPX = False


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed (dev dependency)")
class ApiRoutingTests(unittest.TestCase):
    """Verifies per-path provider routing through the real API: movies go only
    to provider A, series only to provider B. LocalProvider, no network."""

    @classmethod
    def setUpClass(cls):
        init_db()
        cls.movies = tempfile.mkdtemp(prefix="movies-")
        cls.series = tempfile.mkdtemp(prefix="series-")
        cls.target_a = tempfile.mkdtemp(prefix="hostA-")
        cls.target_b = tempfile.mkdtemp(prefix="hostB-")
        with connect() as conn:
            conn.execute("DELETE FROM queue_items")
            conn.execute("DELETE FROM upload_tasks")
            conn.execute("DELETE FROM providers")
            conn.execute("DELETE FROM watch_paths")

    def _write(self, d, name):
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(b"x" * 4096)
        os.utime(p, (1, 1))
        return p

    def test_routing_movies_to_a_series_to_b(self):
        from app.queue import QueueScheduler

        with TestClient(app) as c:
            c.post("/api/pause")  # keep the background scheduler quiet
            a_id = c.post("/api/providers", json={
                "name": "hostA", "type": "local",
                "config": {"target_dir": self.target_a}, "enabled": True}).json()["id"]
            b_id = c.post("/api/providers", json={
                "name": "hostB", "type": "local",
                "config": {"target_dir": self.target_b}, "enabled": True}).json()["id"]
            r = c.post("/api/watchpaths", json={
                "path": self.movies, "enabled": True, "provider_ids": str(a_id)})
            self.assertEqual(r.status_code, 201, r.text)
            r = c.post("/api/watchpaths", json={
                "path": self.series, "enabled": True, "provider_ids": str(b_id)})
            self.assertEqual(r.status_code, 201, r.text)

            self._write(self.movies, "Movie.2024.mkv")
            self._write(self.series, "Show.S01E01.mkv")

            r = c.post("/api/scan")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["started"], True, r.json())
            # Scan runs in the background now; wait for both files to be queued
            deadline = time.time() + 15
            n = 0
            while time.time() < deadline:
                with connect() as conn:
                    n = conn.execute(
                        "SELECT COUNT(*) AS c FROM queue_items").fetchone()["c"]
                if n >= 2:
                    break
                time.sleep(0.2)
            self.assertGreaterEqual(n, 2)

            sched = QueueScheduler(notifier=None, concurrency=2)
            with connect() as conn:
                items = conn.execute(
                    "SELECT id FROM queue_items ORDER BY id").fetchall()
            self.assertGreaterEqual(len(items), 2)
            for it in items:
                sched._process_item(it["id"])

            with connect() as conn:
                for it in items:
                    row = conn.execute(
                        "SELECT status FROM queue_items WHERE id=?",
                        (it["id"],)).fetchone()
                    self.assertEqual(row["status"], "completed")

            cf_m = os.path.basename(self.movies.rstrip(os.sep))
            cf_s = os.path.basename(self.series.rstrip(os.sep))
            self.assertTrue(os.path.exists(
                os.path.join(self.target_a, cf_m, "Movie.2024.mkv")))
            self.assertFalse(os.path.exists(
                os.path.join(self.target_b, cf_m, "Movie.2024.mkv")))
            self.assertTrue(os.path.exists(
                os.path.join(self.target_b, cf_s, "Show.S01E01.mkv")))
            self.assertFalse(os.path.exists(
                os.path.join(self.target_a, cf_s, "Show.S01E01.mkv")))

    def test_deleted_provider_ids_are_filtered(self):
        with TestClient(app) as c:
            c.post("/api/pause")
            pid = c.post("/api/providers", json={
                "name": "only", "type": "local",
                "config": {"target_dir": self.target_a}, "enabled": True}).json()["id"]
            # Routing to a provider id that does NOT exist must be dropped
            # instead of silently wedging items in 'pending' forever.
            r = c.post("/api/watchpaths", json={
                "path": self.movies, "enabled": True,
                "provider_ids": f"{pid},99999"})
            self.assertEqual(r.status_code, 201, r.text)
            saved = next(p for p in c.get("/api/watchpaths").json()["paths"]
                         if p["path"] == os.path.abspath(self.movies))
            self.assertEqual(saved["provider_ids"], str(pid))

    def test_health(self):
        with TestClient(app) as c:
            r = c.get("/api/health")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["ok"], True)

    def test_status_endpoint(self):
        with TestClient(app) as c:
            r = c.get("/api/status")
            self.assertEqual(r.status_code, 200, r.text)
            d = r.json()
            self.assertTrue(d["ok"])
            self.assertIn("watcher", d)
            self.assertIn("scheduler", d)
            self.assertIn("queue", d)
            self.assertIn("db", d)
            self.assertIn("alive", d["watcher"])
            self.assertIn("alive", d["scheduler"])

    def test_logs_endpoint_returns_tail(self):
        # The log tail reader must return only the last N lines even when the
        # log file is large (it reads the tail, not the whole file).
        with open(config.LOG_PATH, "w", encoding="utf-8") as f:
            for i in range(20000):
                f.write(f"line {i}\n")
        with TestClient(app) as c:
            r = c.get("/api/logs?lines=5")
            self.assertEqual(r.status_code, 200)
            lines = r.json()["logs"].strip().splitlines()
            # Exactly `lines` entries, and the tail of the file we wrote is
            # present (the app may append its own startup log lines after).
            self.assertEqual(len(lines), 5)
            self.assertTrue(any(line.startswith("line 199") for line in lines))


if __name__ == "__main__":
    unittest.main(verbosity=2)
