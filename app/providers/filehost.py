import os

from ._util import multipart_monitor, new_session, request_timeout
from .base import BaseProvider


class FileHostProvider(BaseProvider):
    type = "file_host"
    display_name = "File Host API (Xvids-style: StreamHG, EarnVids, Vidoza, ...)"
    field_schema = [
        {"name": "platform", "label": "Platform preset (optional)", "type": "select",
         "options": ["", "StreamHG", "EarnVids", "Vidoza", "LuluStream", "Generic"],
         "help": "Auto-fills the API host. Choose 'Generic' and type the host manually if yours is not listed."},
        {"name": "api_host", "label": "API host", "type": "text", "required": True,
         "help": "e.g. https://streamhgapi.com  (the domain, without /api)"},
        {"name": "api_key", "label": "API key", "type": "password", "required": True},
        {"name": "upload_url", "label": "Upload server URL (optional)", "type": "text",
         "help": "Leave empty to auto-fetch via /api/upload/server"},
        {"name": "fld_id", "label": "Folder ID (optional)", "type": "text"},
        {"name": "cat_id", "label": "Category ID (optional)", "type": "text"},
        {"name": "file_public", "label": "Public", "type": "bool", "default": True},
        {"name": "file_adult", "label": "Adult", "type": "bool", "default": False},
        {"name": "tags", "label": "Tags", "type": "text"},
    ]

    PRESET_HOSTS = {
        "StreamHG": "https://streamhgapi.com",
        "EarnVids": "https://earnvidsapi.com",
        "Vidoza": "https://vidoza.net",
        "LuluStream": "https://api.lulustream.com",
    }

    def __init__(self, config):
        super().__init__(config)
        self._sess = None  # lazy keep-alive session; see _session()
        self._upload_url_cache = None

    def _session(self):
        # One keep-alive session per provider instance, reused across the
        # upload-server fetch, the upload POST and any retries (connection
        # pooling => no repeated TLS handshakes on 1Gbps+ uplinks).
        #
        # NOTE: the backing attribute must have a DIFFERENT name than this
        # method. If it were ``self._session``, the instance attribute would
        # shadow the bound method and every ``self._session()`` call would
        # fail with ``TypeError: 'NoneType' object is not callable``.
        if self._sess is None:
            self._sess = new_session()
        return self._sess

    def _api(self):
        return self.config.get("api_host", "").rstrip("/")

    def _key(self):
        return self.config.get("api_key", "")

    @staticmethod
    def _status_ok(data):
        """Xvids-style APIs report success as status==200, but some clones
        return the code as a string, as True/1, or omit it entirely."""
        s = data.get("status", 200) if isinstance(data, dict) else 200
        if isinstance(s, bool):
            return s
        try:
            return int(s) == 200
        except (TypeError, ValueError):
            return bool(s)

    def _call(self, path, params):
        if not self._api():
            raise RuntimeError("API host is not configured")
        r = self._session().get(self._api() + path, params=params,
                                timeout=request_timeout())
        r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"Invalid API response: {r.text[:200]}") from None
        if not self._status_ok(data):
            raise RuntimeError(f"API error {data.get('status')}: {data.get('msg', '')}")
        return data

    def _resolve_folder_id(self, remote_path):
        cfg_fld = (self.config.get("fld_id") or "").strip()
        if cfg_fld:
            return cfg_fld
        remote_path = (remote_path or "").replace("\\", "/").strip("/")
        parts = remote_path.split("/")
        name = parts[0] if len(parts) > 1 else ""
        if not name:
            return ""
        try:
            data = self._call("/api/folder/list", {"key": self._key()})
        except Exception:
            return ""
        result = data.get("result") or {}
        if isinstance(result, list):
            folders = result
        elif isinstance(result, dict):
            folders = result.get("folders") or []
        else:
            folders = []
        for f in folders:
            if isinstance(f, dict) and str(f.get("name")) == name:
                return str(f.get("fld_id", ""))
        try:
            created = self._call("/api/folder/create",
                                 {"key": self._key(), "name": name})
        except Exception:
            return ""
        res = created.get("result") or {}
        return str(res.get("fld_id", "")) if isinstance(res, dict) else ""

    def _get_upload_url(self):
        url = (self.config.get("upload_url") or "").strip()
        if url:
            return url.rstrip("/")
        if self._upload_url_cache:
            return self._upload_url_cache

        def _extract(data):
            result = data.get("result")
            if isinstance(result, str):
                return result.rstrip("/")
            if isinstance(result, dict):
                for k in ("url", "one_time_upload_link"):
                    if result.get(k):
                        return result[k].rstrip("/")
            return None

        for path in ("/api/upload/server", "/uploadserver"):
            try:
                u = _extract(self._call(path, {"key": self._key()}))
                if u:
                    self._upload_url_cache = u
                    return u
            except Exception:
                continue
        raise RuntimeError("Could not determine upload server URL")

    def test(self):
        try:
            data = self._call("/api/account/info", {"key": self._key()})
            result = data.get("result") or {}
            login = result.get("login") if isinstance(result, dict) else None
            return True, f"Connected as {login}" if login else "Connected"
        except Exception as e:
            try:
                self._get_upload_url()
                return True, "Connected (upload server reachable)"
            except Exception:
                return False, str(e)

    def upload(self, local_path, remote_path, progress_cb, resume_state=None):
        total = os.path.getsize(local_path)
        if total <= 0:
            raise RuntimeError("File is empty")
        upload_url = self._get_upload_url()

        title = os.path.basename(remote_path) or os.path.basename(local_path)
        file_handle = open(local_path, "rb")
        try:
            fields = [
                ("key", self._key()),
                ("file_title", title),
                ("file", (os.path.basename(local_path), file_handle, "application/octet-stream")),
            ]
            fld = self._resolve_folder_id(remote_path)
            if fld:
                fields.append(("fld_id", fld))
            cat = (self.config.get("cat_id") or "").strip()
            if cat:
                fields.append(("cat_id", cat))
            fields.append(("file_public", "1" if self.config.get("file_public", True) else "0"))
            fields.append(("file_adult", "1" if self.config.get("file_adult", False) else "0"))
            tags = (self.config.get("tags") or "").strip()
            if tags:
                fields.append(("tags", tags))

            monitor = multipart_monitor(fields, progress_cb)
            r = self._session().post(
                upload_url, data=monitor,
                headers={"Content-Type": monitor.content_type},
                timeout=request_timeout(),
            )
        finally:
            file_handle.close()

        if r.status_code not in (200, 201):
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"Invalid upload response: {r.text[:200]}") from None
        if not self._status_ok(data):
            raise RuntimeError(f"API error: {data.get('msg', '')}")
        files = data.get("files") or []
        if not files:
            raise RuntimeError("No file entry in upload response")
        for f in files:
            if str(f.get("status", "")).upper() != "OK":
                raise RuntimeError(
                    f"Upload failed for {f.get('filename')}: {f.get('status')}")
