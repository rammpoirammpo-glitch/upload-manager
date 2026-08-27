"""Shared plumbing for provider implementations.

Every HTTP-based provider uses the same connection/read timeouts and the same
multipart-upload machinery, so that a stalled remote host can never freeze a
worker thread and so progress reporting is consistent everywhere.
"""

from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

from .. import config

HEADERS = {"User-Agent": "upload-manager/1.4.0"}


def request_timeout():
    """(connect, read) timeout tuple, read from configuration.

    The read timeout applies per chunk, so slow-but-alive connections keep
    working while fully stalled ones fail and trigger the retry logic.
    """
    return (config.CONNECT_TIMEOUT, config.READ_TIMEOUT)


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
