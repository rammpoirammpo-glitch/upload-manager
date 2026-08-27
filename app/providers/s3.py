"""S3-compatible provider (Backblaze B2, Cloudflare R2, MinIO, ...).

Large files are split into parts (S3_CHUNK_SIZE_MB) and uploaded in parallel
(S3_MAX_CONCURRENCY parts at once) so a 1Gbps+ uplink is saturated instead of
a single sequential stream. Uploads are resumable: the multipart upload id is
persisted in the task state, and a retry/restart lists the already-uploaded
parts from the server and only uploads the missing ones before completing.
"""

import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.client import Config

from .. import config
from .base import BaseProvider

_S3_MIN_PART = 5 * 1024 * 1024  # S3 minimum part size (except the last part)


class S3MultipartError(RuntimeError):
    """Raised when a multipart upload is interrupted; carries the resume
    state (upload id + parts uploaded so far) so the queue can persist it."""

    def __init__(self, message, state=None):
        super().__init__(message)
        self.state = state or {}


def plan_parts(total, chunk_size):
    """Compute the part plan for a file of ``total`` bytes: a list of
    ``(part_number, start_byte, part_size)`` tuples (1-based part numbers)."""
    if total <= 0:
        return []
    n_parts = max(1, math.ceil(total / chunk_size))
    parts = []
    for n in range(1, n_parts + 1):
        start = (n - 1) * chunk_size
        size = min(chunk_size, total - start)
        parts.append((n, start, size))
    return parts


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

    def _require(self, *keys):
        missing = [k for k in keys if not (self.config.get(k) or "").strip()]
        if missing:
            raise ValueError("Missing required S3 setting(s): " + ", ".join(missing))

    def test(self):
        try:
            self._require("access_key", "secret_key", "bucket")
            c = self._client()
            try:
                c.head_bucket(Bucket=self.config["bucket"])
            except Exception:
                c.list_objects_v2(Bucket=self.config["bucket"], MaxKeys=1)
            return True, "Connected"
        except Exception as e:
            return False, str(e)

    # -- resumable parallel multipart ----------------------------------------

    def upload(self, local_path, remote_path, progress_cb, resume_state=None):
        self._require("access_key", "secret_key", "bucket")
        total = os.path.getsize(local_path)
        if total <= 0:
            raise RuntimeError("File is empty")

        chunk = max(_S3_MIN_PART, config.S3_CHUNK_SIZE_MB * 1024 * 1024)
        plan = plan_parts(total, chunk)
        n_parts = len(plan)
        client = self._client()
        bucket = self.config["bucket"]
        key = self._key(remote_path)

        state = dict(resume_state or {})
        upload_id = state.get("upload_id")
        done = {}  # part_number -> ETag

        if upload_id:
            # Resume: ask the server which parts already landed, so we only
            # upload the missing ones (byte-level resume across retries).
            try:
                listed = client.list_parts(Bucket=bucket, Key=key, UploadId=upload_id)
                for p in listed.get("Parts", []):
                    pn = int(p["PartNumber"])
                    if 1 <= pn <= n_parts:
                        done[pn] = p["ETag"]
            except Exception:
                # Upload expired/aborted server-side: start a fresh one.
                upload_id = None
                done = {}
        if not upload_id:
            upload_id = client.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]

        part_size = {n: size for (n, _start, size) in plan}
        uploaded_bytes = sum(part_size[n] for n in done)
        pending = [n for (n, _start, _size) in plan if n not in done]

        def _upload_part(part_number, start, size):
            with open(local_path, "rb") as f:
                f.seek(start)
                body = f.read(size)  # bounded chunk in memory (no file slurp)
            r = client.upload_part(
                Bucket=bucket, Key=key, UploadId=upload_id,
                PartNumber=part_number, Body=body)
            return part_number, r["ETag"]

        workers = max(1, min(config.S3_MAX_CONCURRENCY, len(pending) or 1))
        try:
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="s3part") as ex:
                futures = {ex.submit(_upload_part, n, start, size): n
                           for (n, start, size) in plan if n in pending}
                for fut in as_completed(futures):
                    n = futures[fut]
                    try:
                        pn, etag = fut.result()
                    except Exception:
                        raise S3MultipartError(
                            f"S3 part {n} failed; {len(done)}/{n_parts} parts uploaded",
                            state={"upload_id": upload_id, "parts": done}) from None
                    done[pn] = etag
                    uploaded_bytes += part_size[pn]
                    try:
                        progress_cb(min(1.0, uploaded_bytes / total))
                    except Exception:
                        pass
        except S3MultipartError:
            raise
        except Exception:
            # Worker thread itself blew up (unexpected) — keep partial state.
            raise S3MultipartError(
                f"S3 multipart interrupted; {len(done)}/{n_parts} parts uploaded",
                state={"upload_id": upload_id, "parts": done}) from None

        if len(done) < n_parts:
            raise S3MultipartError(
                f"S3 multipart incomplete ({len(done)}/{n_parts})",
                state={"upload_id": upload_id, "parts": done})

        parts = [{"PartNumber": n, "ETag": done[n]} for n in sorted(done)]
        client.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": parts})
        progress_cb(1.0)
        return None  # fully uploaded and confirmed by the server
