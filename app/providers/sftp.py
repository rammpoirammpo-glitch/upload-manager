import os
import posixpath

import paramiko

from .. import config
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
        """Open an SSH transport with explicit timeouts.

        Without these, a dead remote host can block a worker thread forever
        (paramiko has no default socket deadline), which would consume a
        concurrency slot and eventually freeze the whole queue. The socket and
        channel timeouts turn a stall into an exception the retry logic handles.
        """
        t = paramiko.Transport((self.config["host"], int(self.config.get("port", 22))))
        t.banner_timeout = config.CONNECT_TIMEOUT
        t.auth_timeout = config.CONNECT_TIMEOUT
        t.connect(username=self.config.get("username", ""),
                  password=self.config.get("password", ""))
        try:
            t.sock.settimeout(config.READ_TIMEOUT)
        except Exception:
            pass
        t.set_keepalive(30)
        return t

    def _sftp(self, t):
        sftp = paramiko.SFTPClient.from_transport(t)
        try:
            sftp.get_channel().settimeout(config.READ_TIMEOUT)
        except Exception:
            pass
        return sftp

    def test(self):
        t = None
        try:
            t = self._transport()
            sftp = self._sftp(t)
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

    def upload(self, local_path, remote_path, progress_cb, resume_state=None):
        total = os.path.getsize(local_path)
        if total <= 0:
            raise RuntimeError("File is empty")
        t = self._transport()
        try:
            sftp = self._sftp(t)
            remote = self._remote(remote_path)
            parent = posixpath.dirname(remote)
            if parent and parent != "/":
                self._makedirs(sftp, parent)
            sftp.put(local_path, remote, callback=lambda sent, _t: progress_cb(min(1.0, sent / max(1, total))))
            sftp.close()
        finally:
            t.close()
