import os
import posixpath

import paramiko

from .base import BaseProvider


class SFTPProvider(BaseProvider):
    type = "sftp"
    display_name = "SFTP"
    field_schema = [
        {"name": "host", "label": "Host", "type": "text", "required": True},
        {"name": "port", "label": "Port", "type": "number", "default": 22},
        {"name": "username", "label": "Username", "type": "text", "required": True},
        {"name": "password", "label": "Password", "type": "password", "required": True},
        {"name": "root", "label": "Remote root", "type": "text",
         "help": "Absolute remote directory, e.g. /home/user/uploads"},
    ]

    def _transport(self):
        t = paramiko.Transport((self.config["host"], int(self.config.get("port", 22))))
        t.connect(username=self.config.get("username", ""), password=self.config.get("password", ""))
        return t

    def test(self):
        t = None
        try:
            t = self._transport()
            sftp = paramiko.SFTPClient.from_transport(t)
            sftp.listdir(self.config.get("root", "") or "/")
            sftp.close()
            return True, "Connected"
        except Exception as e:
            return False, str(e)
        finally:
            if t is not None:
                t.close()

    def _remote(self, remote_path):
        root = self.config.get("root", "") or ""
        return posixpath.join(root, remote_path)

    def _makedirs(self, sftp, path):
        parts = [p for p in path.split("/") if p]
        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                sftp.stat(cur)
            except OSError:
                try:
                    sftp.mkdir(cur)
                except OSError:
                    pass

    def upload(self, local_path, remote_path, progress_cb):
        total = os.path.getsize(local_path)
        if total <= 0:
            raise RuntimeError("File is empty")
        t = self._transport()
        try:
            sftp = paramiko.SFTPClient.from_transport(t)
            remote = self._remote(remote_path)
            parent = posixpath.dirname(remote)
            if parent and parent != "/":
                self._makedirs(sftp, parent)
            sftp.put(local_path, remote, callback=lambda sent, _t: progress_cb(min(1.0, sent / max(1, total))))
            sftp.close()
        finally:
            t.close()
