import json
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import config
from .database import connect, get_setting, set_setting
from .providers import get_all_provider_classes, get_provider_class

router = APIRouter()


class ProviderIn(BaseModel):
    name: str
    type: str
    config: dict = Field(default_factory=dict)
    enabled: bool = True


class WatchPathIn(BaseModel):
    path: str = ""
    enabled: bool = True
    remote_dir: str = ""


def _serialize_provider(row):
    d = dict(row)
    try:
        d["config"] = json.loads(d.get("config") or "{}")
    except Exception:
        d["config"] = {}
    d["enabled"] = bool(d.get("enabled"))
    return d


@router.get("/api/provider-types")
def provider_types():
    return {"types": [
        {"type": c.type, "display_name": c.display_name, "fields": c.field_schema}
        for c in get_all_provider_classes()
    ]}


@router.get("/api/providers")
def list_providers():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM providers ORDER BY id").fetchall()
    return {"providers": [_serialize_provider(r) for r in rows]}


@router.post("/api/providers", status_code=201)
def create_provider(body: ProviderIn):
    if get_provider_class(body.type) is None:
        raise HTTPException(400, f"Unknown provider type: {body.type}")
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO providers(name,type,config,enabled) VALUES(?,?,?,?)",
            (body.name, body.type, json.dumps(body.config),
             1 if body.enabled else 0))
        row = conn.execute(
            "SELECT * FROM providers WHERE id=?", (cur.lastrowid,)).fetchone()
    return _serialize_provider(row)


@router.put("/api/providers/{pid}")
def update_provider(pid: int, body: ProviderIn):
    with connect() as conn:
        row = conn.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "Provider not found")
        conn.execute(
            "UPDATE providers SET name=?, type=?, config=?, enabled=? WHERE id=?",
            (body.name, body.type, json.dumps(body.config),
             1 if body.enabled else 0, pid))
        row = conn.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
    return _serialize_provider(row)


@router.delete("/api/providers/{pid}")
def delete_provider(pid: int):
    with connect() as conn:
        conn.execute("DELETE FROM upload_tasks WHERE provider_id=?", (pid,))
        conn.execute("DELETE FROM providers WHERE id=?", (pid,))
    return {"ok": True}


@router.post("/api/providers/{pid}/test")
def test_provider(pid: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
    if not row:
        raise HTTPException(404, "Provider not found")
    cls = get_provider_class(row["type"])
    if cls is None:
        raise HTTPException(400, "Unknown provider type")
    try:
        ok, msg = cls(json.loads(row["config"] or "{}")).test()
    except Exception as e:
        ok, msg = False, str(e)
    return {"ok": ok, "message": msg}


@router.get("/api/watchpaths")
def list_watchpaths():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM watch_paths ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["exists"] = os.path.isdir(d["path"])
        effective = d["remote_dir"] or ""
        if not effective and config.AUTO_REMOTE_FOLDER:
            effective = os.path.basename(d["path"].rstrip(os.sep)) or ""
        d["effective_remote_dir"] = effective
        out.append(d)
    return {"paths": out}


@router.post("/api/watchpaths", status_code=201)
def create_watchpath(body: WatchPathIn):
    if not (body.path or "").strip():
        raise HTTPException(400, "Path is required")
    p = os.path.abspath(body.path)
    remote_dir = (body.remote_dir or "").strip().strip("/")
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM watch_paths WHERE path=?", (p,)).fetchone()
        if existing:
            # The path is already configured: just update its remote folder
            # (and enabled state) instead of rejecting with 409.
            conn.execute(
                "UPDATE watch_paths SET remote_dir=?, enabled=? WHERE id=?",
                (remote_dir, 1 if body.enabled else 0, existing["id"]))
            row = conn.execute(
                "SELECT * FROM watch_paths WHERE id=?", (existing["id"],)).fetchone()
            return dict(row)
        conn.execute(
            "INSERT INTO watch_paths(path, enabled, remote_dir) VALUES(?,?,?)",
            (p, 1 if body.enabled else 0, remote_dir))
        row = conn.execute("SELECT * FROM watch_paths WHERE path=?", (p,)).fetchone()
    return dict(row)


@router.put("/api/watchpaths/{pid}")
def update_watchpath(pid: int, body: WatchPathIn):
    remote_dir = (body.remote_dir or "").strip().strip("/")
    with connect() as conn:
        row = conn.execute("SELECT * FROM watch_paths WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "Watch path not found")
        conn.execute(
            "UPDATE watch_paths SET enabled=?, remote_dir=? WHERE id=?",
            (1 if body.enabled else 0, remote_dir, pid))
        row = conn.execute("SELECT * FROM watch_paths WHERE id=?", (pid,)).fetchone()
    return dict(row)


@router.delete("/api/watchpaths/{pid}")
def delete_watchpath(pid: int):
    with connect() as conn:
        conn.execute("DELETE FROM watch_paths WHERE id=?", (pid,))
    return {"ok": True}


def _item_with_tasks(row):
    item = dict(row)
    with connect() as conn:
        tasks = conn.execute(
            "SELECT t.id AS task_id, t.provider_id, t.status, t.progress, "
            "t.attempts, t.error, p.name AS provider_name "
            "FROM upload_tasks t LEFT JOIN providers p ON p.id=t.provider_id "
            "WHERE t.item_id=? ORDER BY t.id", (item["id"],)).fetchall()
    item["tasks"] = [dict(t) for t in tasks]
    return item


@router.get("/api/queue")
def list_queue(status: str = None, limit: int = 500):
    q = "SELECT * FROM queue_items"
    params = []
    if status:
        q += " WHERE status=?"
        params.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(min(limit, 2000))
    with connect() as conn:
        rows = conn.execute(q, params).fetchall()
    return {"items": [_item_with_tasks(r) for r in rows]}


@router.post("/api/queue/{qid}/retry")
def retry_item(qid: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM queue_items WHERE id=?", (qid,)).fetchone()
        if not row:
            raise HTTPException(404, "Item not found")
        if row["status"] == "uploading":
            raise HTTPException(409, "Item is still uploading; wait for it to finish")
        conn.execute(
            "UPDATE upload_tasks SET status='pending', progress=0, error=NULL, "
            "attempts=0 WHERE item_id=?", (qid,))
        conn.execute(
            "UPDATE queue_items SET status='pending', error=NULL, attempts=0, "
            "updated_at=datetime('now') WHERE id=?", (qid,))
    return {"ok": True}


@router.post("/api/queue/{qid}/skip")
def skip_item(qid: int):
    with connect() as conn:
        conn.execute(
            "UPDATE upload_tasks SET status='skipped', error='Skipped by user' "
            "WHERE item_id=? AND status='failed'", (qid,))
        conn.execute(
            "UPDATE queue_items SET status='skipped', error=NULL, "
            "updated_at=datetime('now') WHERE id=?", (qid,))
    return {"ok": True}


@router.delete("/api/queue/{qid}")
def delete_item(qid: int):
    with connect() as conn:
        conn.execute("DELETE FROM queue_items WHERE id=?", (qid,))
    return {"ok": True}


@router.post("/api/queue/clear")
def clear_queue(status: str = "completed"):
    with connect() as conn:
        rows = conn.execute("SELECT id FROM queue_items WHERE status=?", (status,)).fetchall()
        for r in rows:
            conn.execute("DELETE FROM queue_items WHERE id=?", (r["id"],))
    return {"ok": True, "removed": len(rows)}


@router.get("/api/stats")
def stats():
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM queue_items GROUP BY status").fetchall()
        providers = conn.execute("SELECT COUNT(*) AS c FROM providers").fetchone()["c"]
        paths = conn.execute("SELECT COUNT(*) AS c FROM watch_paths").fetchone()["c"]
    counts = {r["status"]: r["c"] for r in rows}
    return {"counts": counts, "providers": providers, "watch_paths": paths}


@router.get("/api/logs")
def get_logs(lines: int = 300):
    try:
        with open(config.LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            data = f.readlines()
        return {"logs": "".join(data[-lines:])}
    except FileNotFoundError:
        return {"logs": ""}


@router.post("/api/scan")
def manual_scan(request: Request):
    result = request.app.state.watcher.scan_once()
    return {"ok": True, **result}


@router.post("/api/pause")
def pause(request: Request):
    request.app.state.scheduler.paused = True
    return {"ok": True}


@router.post("/api/resume")
def resume(request: Request):
    request.app.state.scheduler.paused = False
    return {"ok": True}


@router.get("/api/notify/status")
def notify_status(request: Request):
    n = request.app.state.notifier
    return {"enabled": n.enabled, "chat_id": n.chat_id or None}


@router.post("/api/notify/test")
def notify_test(request: Request):
    n = request.app.state.notifier
    ok, msg = n.test()
    return {"ok": ok, "message": msg}


class SettingsIn(BaseModel):
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


@router.get("/api/settings")
def get_settings():
    return {
        "telegram_bot_token": get_setting("telegram_bot_token", ""),
        "telegram_chat_id": get_setting("telegram_chat_id", ""),
        "delete_after_upload": config.DELETE_AFTER_UPLOAD,
    }


@router.put("/api/settings")
def update_settings(body: SettingsIn):
    set_setting("telegram_bot_token", body.telegram_bot_token.strip())
    set_setting("telegram_chat_id", body.telegram_chat_id.strip())
    return {"ok": True}
