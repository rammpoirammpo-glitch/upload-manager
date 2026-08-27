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
        dest = os.path.join(self.config.get("target_dir", ""), remote_path.replace("/", os.sep))
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
