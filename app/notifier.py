import logging
import threading

import requests

from . import config

logger = logging.getLogger("uploader.notifier")


class Notifier:
    def __init__(self):
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)
        if self.enabled:
            logger.info("Telegram notifications enabled")
        else:
            logger.info(
                "Telegram notifications disabled "
                "(set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")

    def _post(self, text):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text,
                      "disable_web_page_preview": True},
                timeout=10,
            )
            if r.status_code != 200:
                logger.warning("Telegram send failed: HTTP %s %s",
                               r.status_code, r.text[:200])
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)

    def notify(self, text):
        if not self.enabled:
            return
        threading.Thread(target=self._post, args=(text,), daemon=True).start()

    def test(self):
        if not self.enabled:
            return False, "Telegram is not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)"
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id,
                      "text": "Upload Manager: Telegram notifications are working"},
                timeout=10,
            )
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}: {r.text[:200]}"
            return True, "Message sent"
        except Exception as e:
            return False, str(e)
