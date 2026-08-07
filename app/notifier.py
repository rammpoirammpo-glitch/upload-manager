import logging
import threading

import requests

from . import config
from .database import get_setting

logger = logging.getLogger("uploader.notifier")


def _load_creds():
    """Return (token, chat_id). Values set in the web UI (DB) take priority
    over the legacy environment variables."""
    token = get_setting("telegram_bot_token", "").strip() or config.TELEGRAM_BOT_TOKEN.strip()
    chat = get_setting("telegram_chat_id", "").strip() or config.TELEGRAM_CHAT_ID.strip()
    return token, chat


class Notifier:
    def __init__(self):
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

    def _post(self, token, chat, text):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text,
                      "disable_web_page_preview": True},
                timeout=10,
            )
            if r.status_code != 200:
                logger.warning("Telegram send failed: HTTP %s %s",
                               r.status_code, r.text[:200])
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)

    def notify(self, text):
        token, chat = _load_creds()
        if not (token and chat):
            return
        threading.Thread(target=self._post, args=(token, chat, text),
                         daemon=True).start()

    def test(self):
        token, chat = _load_creds()
        if not (token and chat):
            return False, "Telegram is not configured (set the token in Settings)"
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat,
                      "text": "Upload Manager: Telegram notifications are working"},
                timeout=10,
            )
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}: {r.text[:200]}"
            return True, "Message sent"
        except Exception as e:
            return False, str(e)
