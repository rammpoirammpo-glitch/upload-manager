import os

import requests
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

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
        "LuluStream": "https://lulustream.com",
    }

    def _api(self):
        return self.config.get("api_host", "").rstrip("/")

    def _key(self):
        return self.config.get("api_key", "")

    def _call(self, path, params):
        r = requests.get(self._api() + path, params=params, timeout=20,
                         headers={"User-Agent": "upload-manager/1.0"})
        r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"Invalid API response: {r.text[:200]}")
        if int(data.get("status", 200)) != 200:
            raise RuntimeError(f"API error {data.get('status')}: {data.get('msg', '')}")
        return data

    def _resolve_folder_id(self, remote_path):
        cfg_fld = (self.config.get("fld_id") or "").strip()
        if cfg_fld:
            return cfg_fld
        remote_path = (remote_path or "").replace("\\", "/").strip("/")
        name = remote_path.split("/")[0] if remote_path else ""
        if not name:
            return ""
        try:
            data = self._call("/api/folder/list", {"key": self._key()})
        except Exception:
            return ""
        result = data.get("result") or {}
        folders = (result.get("folders") or []) if isinstance(result, dict) else []
        for f in folders:
            if str(f.get("name")) == name:
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

    def upload(self, local_path, remote_path, progress_cb):
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

            enc = MultipartEncoder(fields=fields)

            def _monitor_cb(monitor):
                try:
                    progress_cb(min(1.0, monitor.bytes_read / max(1, monitor.len)))
                except Exception:
                    pass

            monitor = MultipartEncoderMonitor(enc, _monitor_cb)
            r = requests.post(
                upload_url, data=monitor,
                headers={"Content-Type": monitor.content_type,
                         "User-Agent": "upload-manager/1.0"},
                timeout=(30, 7200),
            )
        finally:
            file_handle.close()

        if r.status_code not in (200, 201):
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"Invalid upload response: {r.text[:200]}")
        if int(data.get("status", 200)) != 200:
            raise RuntimeError(f"API error: {data.get('msg', '')}")
        files = data.get("files") or []
        if not files:
            raise RuntimeError("No file entry in upload response")
        for f in files:
            if str(f.get("status", "")).upper() != "OK":
                raise RuntimeError(
                    f"Upload failed for {f.get('filename')}: {f.get('status')}")
