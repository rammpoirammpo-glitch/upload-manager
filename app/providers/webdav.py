import os

import requests

from .base import BaseProvider


class WebDAVProvider(BaseProvider):
    type = "webdav"
    display_name = "WebDAV"
    field_schema = [
        {"name": "url", "label": "Base URL", "type": "text", "required": True,
         "help": "e.g. https://dav.example.com/files/user"},
        {"name": "root", "label": "Remote root", "type": "text",
         "help": "Path prefix in storage, e.g. uploads"},
        {"name": "username", "label": "Username", "type": "text", "required": True},
        {"name": "password", "label": "Password", "type": "password", "required": True},
        {"name": "verify_ssl", "label": "Verify SSL", "type": "bool", "default": True},
    ]

    def _auth(self):
        return (self.config.get("username", ""), self.config.get("password", ""))

    def _base(self):
        return self.config.get("url", "").rstrip("/")

    def _verify(self):
        return bool(self.config.get("verify_ssl", True))

    def _remote(self, remote_path):
        root = self.config.get("root", "").strip("/")
        parts = [p for p in (root, remote_path) if p]
        return self._base() + "/" + "/".join(parts)

    def test(self):
        try:
            r = requests.request(
                "PROPFIND", self._base() + "/",
                auth=self._auth(), headers={"Depth": "0"},
                timeout=15, verify=self._verify(),
            )
            if r.status_code in (200, 207):
                return True, "Connected"
            if r.status_code in (401, 403):
                return False, f"Authentication failed (HTTP {r.status_code})"
            return False, f"Unexpected status (HTTP {r.status_code})"
        except Exception as e:
            return False, str(e)

    def _ensure_dirs(self, session, remote_dir):
        if not remote_dir:
            return
        cur = self._base()
        for part in remote_dir.split("/"):
            if not part:
                continue
            cur += "/" + part
            try:
                session.request("MKCOL", cur, timeout=20)
            except Exception:
                pass

    def upload(self, local_path, remote_path, progress_cb):
        total = os.path.getsize(local_path)
        if total <= 0:
            raise RuntimeError("File is empty")
        with requests.Session() as s:
            s.auth = self._auth()
            s.verify = self._verify()
            remote_dir = remote_path.rsplit("/", 1)[0] if "/" in remote_path else ""
            self._ensure_dirs(s, remote_dir)

            class ProgressFile:
                def __init__(self):
                    self.f = open(local_path, "rb")
                    self.sent = 0

                def read(self, n):
                    data = self.f.read(n)
                    if data:
                        self.sent += len(data)
                        progress_cb(self.sent / total)
                    return data

                def close(self):
                    self.f.close()

            r = s.put(
                self._remote(remote_path),
                data=ProgressFile(),
                headers={"Content-Length": str(total)},
                timeout=(30, 7200),
            )
            if r.status_code not in (200, 201, 204):
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
