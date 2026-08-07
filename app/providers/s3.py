import os

import boto3
from botocore.client import Config

from .base import BaseProvider


class S3Provider(BaseProvider):
    type = "s3"
    display_name = "S3 compatible (Backblaze B2, R2, MinIO, ...)"
    field_schema = [
        {"name": "endpoint", "label": "Endpoint URL", "type": "text",
         "help": "e.g. https://s3.us-west-004.backblazeb2.com"},
        {"name": "region", "label": "Region", "type": "text", "help": "Optional"},
        {"name": "access_key", "label": "Access Key ID", "type": "text", "required": True},
        {"name": "secret_key", "label": "Secret Access Key", "type": "password", "required": True},
        {"name": "bucket", "label": "Bucket", "type": "text", "required": True},
        {"name": "root", "label": "Path prefix", "type": "text", "help": "e.g. uploads"},
        {"name": "path_style", "label": "Path-style addressing", "type": "bool", "default": True},
    ]

    def _client(self):
        cfg = self.config
        return boto3.client(
            "s3",
            endpoint_url=cfg.get("endpoint") or None,
            region_name=cfg.get("region") or None,
            aws_access_key_id=cfg.get("access_key", ""),
            aws_secret_access_key=cfg.get("secret_key", ""),
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if cfg.get("path_style", True) else "auto"},
            ),
        )

    def _key(self, remote_path):
        root = self.config.get("root", "").strip("/")
        return root + "/" + remote_path if root else remote_path

    def test(self):
        try:
            c = self._client()
            try:
                c.head_bucket(Bucket=self.config["bucket"])
            except Exception:
                c.list_objects_v2(Bucket=self.config["bucket"], MaxKeys=1)
            return True, "Connected"
        except Exception as e:
            return False, str(e)

    def upload(self, local_path, remote_path, progress_cb):
        class _Progress:
            def __init__(self):
                self.seen = 0
                self.total = os.path.getsize(local_path)

            def __call__(self, n):
                self.seen += n
                progress_cb(min(1.0, self.seen / max(1, self.total)))

        self._client().upload_file(
            local_path, self.config["bucket"], self._key(remote_path), Callback=_Progress()
        )
