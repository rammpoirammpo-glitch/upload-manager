import os

import requests

from ._util import HEADERS, multipart_monitor, request_timeout
from .base import BaseProvider

DEFAULT_UPLOAD_URL = "https://ul.mixdrop.ag/api"


class MixdropProvider(BaseProvider):
    type = "mixdrop"
    display_name = "Mixdrop"
    field_schema = [
        {"name": "email", "label": "Email", "type": "text", "required": True},
        {"name": "key", "label": "API key", "type": "password", "required": True,
         "help": "Grab your API key from https://mixdrop.ag/api"},
        {"name": "upload_url", "label": "Upload URL (optional)", "type": "text",
         "default": DEFAULT_UPLOAD_URL},
        {"name": "folder_id", "label": "Folder ID (optional)", "type": "text"},
    ]

    def _upload_url(self):
        return (self.config.get("upload_url") or DEFAULT_UPLOAD_URL).rstrip("/")

    def test(self):
        email = (self.config.get("email") or "").strip()
        key = (self.config.get("key") or "").strip()
        if not email or not key:
            return False, "Email and API key are required"
        try:
            # folderlist returns success=true only when the key is valid.
            r = requests.get("https://api.mixdrop.ag/folderlist",
                             params={"email": email, "key": key},
                             timeout=request_timeout(), headers=HEADERS)
            r.raise_for_status()
            data = r.json()
            if not data.get("success"):
                return False, f"Invalid credentials: {data.get('msg', '')}"
            return True, "Connected to Mixdrop"
        except Exception as e:
            return False, str(e)

    def upload(self, local_path, remote_path, progress_cb):
        total = os.path.getsize(local_path)
        if total <= 0:
            raise RuntimeError("File is empty")
        email = (self.config.get("email") or "").strip()
        key = (self.config.get("key") or "").strip()
        if not email or not key:
            raise RuntimeError("Email and API key are required")

        fh = open(local_path, "rb")
        try:
            fields = [
                ("email", email),
                ("key", key),
                ("file", (os.path.basename(local_path), fh, "application/octet-stream")),
            ]
            fld = (self.config.get("folder_id") or "").strip()
            if fld:
                fields.append(("folder", fld))

            monitor = multipart_monitor(fields, progress_cb)
            r = requests.post(
                self._upload_url(), data=monitor,
                headers={"Content-Type": monitor.content_type, **HEADERS},
                timeout=request_timeout(),
            )
        finally:
            fh.close()

        if r.status_code not in (200, 201):
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"Invalid upload response: {r.text[:200]}") from None
        if not data.get("success"):
            raise RuntimeError(f"API error: {data.get('msg', '')}")
        res = data.get("result")
        if not isinstance(res, dict) or not (res.get("url") or res.get("fileref")):
            raise RuntimeError(f"Empty upload result from Mixdrop: {res or ''}")
