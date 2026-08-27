"""Shared plumbing for provider implementations.

Every HTTP-based provider uses the same connection/read timeouts, the same
multipart-upload machinery and the same keep-alive connection pool, so that a
stalled remote host can never freeze a worker thread, latency from repeated
handshakes is eliminated, and progress reporting is consistent everywhere.
"""

import requests
from requests.adapters import HTTPAdapter
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

from .. import config

HEADERS = {"User-Agent": "upload-manager/1.5.0"}


def request_timeout():
    """(connect, read) timeout tuple, read from configuration.

    The read timeout applies per chunk, so slow-but-alive connections keep
    working while fully stalled ones fail and trigger the retry logic.
    """
    return (config.CONNECT_TIMEOUT, config.READ_TIMEOUT)


def sanitize_path(path):
    """Sanitize a user-supplied remote path for path-traversal safety.

    Drops empty, ``.`` and ``..`` segments so a watch-path remote folder like
    ``../../etc`` can never escape the intended cloud root (LocalProvider,
    WebDAV, SFTP) or inject ``..`` into an S3 key.
    """
    return "/".join(seg for seg in (path or "").split("/")
                     if seg not in ("", ".", ".."))


def new_session():
    """A requests.Session with a connection pool and keep-alive enabled.

    The session reuses TCP/TLS connections across calls to the same host, so
    the upload-server fetch, the multipart POST and any retries do not pay a
    new handshake each time. ``max_retries=0`` because the upload queue owns
    the retry policy (exponential backoff via tenacity).
    """
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=config.HTTP_POOL_CONNECTIONS,
        pool_maxsize=config.HTTP_POOL_MAXSIZE,
        max_retries=0,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session


def multipart_monitor(fields, progress_cb):
    """Build a ``MultipartEncoderMonitor`` that reports upload progress.

    ``fields`` is passed straight to ``MultipartEncoder`` (a list of tuples or
    a dict); ordering is preserved, which some hosts rely on.
    """
    encoder = MultipartEncoder(fields=fields)

    def _monitor(monitor):
        try:
            progress_cb(min(1.0, monitor.bytes_read / max(1, monitor.len)))
        except Exception:
            pass

    return MultipartEncoderMonitor(encoder, _monitor)
