import os

from .base import BaseProvider


class LocalProvider(BaseProvider):
    type = "local"
    display_name = "Local folder (copy)"
    field_schema = [
        {"name": "target_dir", "label": "Target directory", "type": "text", "required": True,
         "help": "e.g. /backup/movies - useful for testing"},
    ]

    def test(self):
        d = self.config.get("target_dir", "")
        try:
            os.makedirs(d, exist_ok=True)
            return True, f"Directory ready: {d}"
        except Exception as e:
            return False, str(e)

    def upload(self, local_path, remote_path, progress_cb, resume_state=None):
        target = self.config.get("target_dir", "")
        dest = os.path.join(target, remote_path.replace("/", os.sep))
        # Path-traversal guard: even if remote_path somehow contained "..",
        # we refuse to write outside the configured target directory.
        real_target = os.path.realpath(target)
        real_dest = os.path.realpath(dest)
        if not (real_dest == real_target or real_dest.startswith(real_target + os.sep)):
            raise RuntimeError(f"Refusing to write outside target directory: {dest}")
        # Never overwrite the source file itself (e.g. target == source dir).
        if os.path.abspath(dest) == os.path.abspath(local_path):
            raise RuntimeError("Refusing to overwrite the source file itself")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        total = os.path.getsize(local_path)
        if total <= 0:
            raise RuntimeError("File is empty")
        copied = 0
        with open(local_path, "rb") as src, open(dest, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                copied += len(chunk)
                progress_cb(copied / total)
        progress_cb(1.0)
