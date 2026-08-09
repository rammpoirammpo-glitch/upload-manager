import hashlib
import os

import requests
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

from .base import BaseProvider

API_BASE = "https://api.streamtape.com"


class StreamTapeProvider(BaseProvider):
    type = "streamtape"
    display_name = "StreamTape"
    field_schema = [
        {"name": "login", "label": "API Login", "type": "text", "required": True,
         "help": "From User Panel > Account Settings (API/FTP Credentials)"},
        {"name": "key", "label": "API Key", "type": "password", "required": True,
         "help": "API-Key / API-Password from Account Settings"},
        {"name": "folder", "label": "Folder ID (optional)", "type": "text"},
    ]

    def _sha256(self, path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _get_upload_url(self, local_path):
        params = {
            "login": (self.config.get("login") or "").strip(),
            "key": (self.config.get("key") or "").strip(),
            "sha256": self._sha256(local_path),
        }
        fld = (self.config.get("folder") or "").strip()
        if fld:
            params["folder"] = fld
        r = requests.get(API_BASE + "/file/ul", params=params, timeout=30,
                         headers={"User-Agent": "upload-manager/1.0"})
        r.raise_for_status()
        data = r.json()
        if int(data.get("status", 0)) != 200:
            raise RuntimeError("API error: %s" % data.get("msg", ""))
        url = (data.get("result") or {}).get("url")
        if not url:
            raise RuntimeError("No upload URL in StreamTape response")
        return url

    def test(self):
        login = (self.config.get("login") or "").strip()
        key = (self.config.get("key") or "").strip()
        if not login or not key:
            return False, "API Login and API Key are required"
        try:
            r = requests.get(API_BASE + "/account/info",
                             params={"login": login, "key": key}, timeout=20,
                             headers={"User-Agent": "upload-manager/1.0"})
            r.raise_for_status()
            data = r.json()
            if int(data.get("status", 0)) != 200:
                return False, "Invalid credentials: %s" % data.get("msg", "")
            res = data.get("result") or {}
            email = res.get("email") if isinstance(res, dict) else ""
            return True, "Connected%s" % (" as %s" % email if email else "")
        except Exception as e:
            return False, str(e)

    def upload(self, local_path, remote_path, progress_cb):
        total = os.path.getsize(local_path)
        if total <= 0:
            raise RuntimeError("File is empty")
        upload_url = self._get_upload_url(local_path)

        fh = open(local_path, "rb")
        try:
            enc = MultipartEncoder({
                "file1": (os.path.basename(local_path), fh, "application/octet-stream"),
            })

            def _monitor(mon):
                try:
                    progress_cb(min(1.0, mon.bytes_read / max(1, mon.len)))
                except Exception:
                    pass

            monitor = MultipartEncoderMonitor(enc, _monitor)
            r = requests.post(
                upload_url, data=monitor,
                headers={"Content-Type": monitor.content_type,
                         "User-Agent": "upload-manager/1.0"},
                timeout=(30, 7200),
            )
        finally:
            fh.close()

        if r.status_code not in (200, 201):
            raise RuntimeError("HTTP %s: %s" % (r.status_code, r.text[:200]))
        try:
            data = r.json()
        except Exception:
            raise RuntimeError("Invalid upload response: %s" % r.text[:200])
        if int(data.get("status", 0)) != 200:
            raise RuntimeError("Upload error: %s" % data.get("msg", ""))
