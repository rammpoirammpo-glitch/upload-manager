"""Telegram notifications for the upload-manager daemon.

Design goals (production / 24-7 operation):
- ``notify()`` never blocks the caller: messages go to a bounded queue
  consumed by ONE background worker thread (no thread-per-message).
- Messages are rate-limited and coalesced per content key, so a failing
  provider or a repeated error can never flood Telegram.
- ``TelegramLogHandler`` bridges the logging system to Telegram: every
  WARNING/ERROR log record is forwarded (rate-limited) so the operator is
  alerted to critical problems while away from the server.
"""

import logging
import queue as _queue
import threading
import time

import requests

from . import config
from .database import get_setting

logger = logging.getLogger("uploader.notifier")

_TG_API = "https://api.telegram.org/bot{token}/sendMessage"


def _load_creds():
    """Return (token, chat_id). Values set in the web UI (DB) take priority
    over the legacy environment variables."""
    token = get_setting("telegram_bot_token", "").strip() or config.TELEGRAM_BOT_TOKEN.strip()
    chat = get_setting("telegram_chat_id", "").strip() or config.TELEGRAM_CHAT_ID.strip()
    return token, chat


class TelegramLogHandler(logging.Handler):
    """A logging handler that forwards WARNING+ records to Telegram.

    Attach it to the root logger once the Notifier exists (see app.main
    lifespan). Formatting is light on purpose: the message text itself is
    what matters on a phone screen.
    """

    def __init__(self, notifier):
        super().__init__(level=logging.WARNING)
        self.notifier = notifier
        self.setFormatter(logging.Formatter(
            "[%(levelname)s] %(name)s: %(message)s"))

    def emit(self, record):
        if record.name == __name__:  # never forward our own diagnostics
            return
        try:
            msg = self.format(record)
        except Exception:
            return
        self.notifier.notify(msg, level=record.levelname)


class Notifier:
    """Rate-limited, non-blocking Telegram notifier on one worker thread."""

    # Keep the in-memory cooldown table bounded.
    _COOLDOWN_SECONDS = 60.0
    _MAX_TRACKED_KEYS = 512

    def __init__(self):
        self._queue = _queue.Queue(maxsize=200)
        self._last_sent = {}
        self._lock = threading.Lock()
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="telegram-worker")
        self._thread.start()
        self.log_handler = TelegramLogHandler(self)
        if self.enabled:
            logger.info("Telegram notifications enabled")
        else:
            logger.info(
                "Telegram notifications disabled "
                "(set the token in the dashboard Settings tab)")

    @property
    def enabled(self):
        token, chat = _load_creds()
        return bool(token and chat)

    @property
    def chat_id(self):
        return _load_creds()[1] or None

    # -- internal ------------------------------------------------------------

    def _run(self):
        """Single consumer: keeps exactly one HTTP connection at a time and
        never lets a slow Telegram API block the upload workers."""
        while self._running:
            try:
                token, chat, text = self._queue.get(timeout=1.0)
            except _queue.Empty:
                continue
            try:
                self._send(token, chat, text)
            except Exception:
                logger.exception("Telegram worker crashed")

    def _send(self, token, chat, text):
        r = requests.post(
            _TG_API.format(token=token),
            json={"chat_id": chat, "text": text,
                  "disable_web_page_preview": True},
            timeout=(10, 30),
        )
        if r.status_code != 200:
            logger.warning("Telegram send failed: HTTP %s %s",
                           r.status_code, r.text[:200])

    def _cooldown_key(self, text):
        return text.strip()[:80]

    def _allow(self, key):
        """True if a message with this key may be sent now (rate limit)."""
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(key, 0.0)
            if now - last < self._COOLDOWN_SECONDS:
                return False
            self._last_sent[key] = now
            if len(self._last_sent) > self._MAX_TRACKED_KEYS:
                cutoff = now - 3600
                for k in [k for k, ts in self._last_sent.items() if ts < cutoff]:
                    self._last_sent.pop(k, None)
            return True

    # -- public API ----------------------------------------------------------

    def notify(self, text, level="INFO", key=None):
        """Queue a message for delivery (non-blocking, rate-limited)."""
        token, chat = _load_creds()
        if not (token and chat):
            return
        if not self._allow(key or self._cooldown_key(text)):
            return
        try:
            self._queue.put_nowait((token, chat, text))
        except _queue.Full:
            logger.warning("Telegram queue full; dropping message: %.80s", text)

    def test(self):
        """Send a test message synchronously (bypasses queue + rate limit)."""
        token, chat = _load_creds()
        if not (token and chat):
            return False, "Telegram is not configured (set the token in Settings)"
        try:
            r = requests.post(
                _TG_API.format(token=token),
                json={"chat_id": chat,
                      "text": "Upload Manager: Telegram notifications are working"},
                timeout=(10, 30),
            )
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}: {r.text[:200]}"
            return True, "Message sent"
        except Exception as e:
            return False, str(e)

    def shutdown(self):
        """Stop the worker thread (called on daemon shutdown)."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3)
