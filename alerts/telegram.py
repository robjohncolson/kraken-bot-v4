from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeAlias
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from alerts.formatter import AlertType, MessageFormatter

logger = logging.getLogger(__name__)

Sender: TypeAlias = Callable[[Request, float], None]


class TelegramAlertError(ConnectionError):
    """Base exception for Telegram delivery failures."""


class TelegramDeliveryError(TelegramAlertError):
    """Raised when the Telegram API request cannot be completed."""


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: str | None
    chat_id: str | None

    @property
    def is_configured(self) -> bool:
        return self.bot_token is not None and self.chat_id is not None


class TelegramAlerter:
    """Send formatted alerts to Telegram when credentials are configured."""

    def __init__(
        self,
        *,
        bot_token: str | None = None,
        chat_id: str | None = None,
        environ: Mapping[str, str] | None = None,
        formatter: MessageFormatter | None = None,
        sender: Sender | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        environment = os.environ if environ is None else environ
        self._config = TelegramConfig(
            bot_token=_clean_value(
                bot_token if bot_token is not None else environment.get("TELEGRAM_BOT_TOKEN")
            ),
            chat_id=_clean_value(
                chat_id if chat_id is not None else environment.get("TELEGRAM_CHAT_ID")
            ),
        )
        self._formatter = MessageFormatter() if formatter is None else formatter
        self._sender = _default_sender if sender is None else sender
        self._timeout_seconds = timeout_seconds

    @property
    def is_configured(self) -> bool:
        return self._config.is_configured

    def send_alert(
        self,
        alert_type: str | AlertType,
        details: Mapping[str, object],
    ) -> bool:
        if not self.is_configured:
            logger.warning(
                "Telegram alerts are disabled because TELEGRAM_BOT_TOKEN or "
                "TELEGRAM_CHAT_ID is not set."
            )
            return False
        return self.send_message(self._formatter.format(alert_type, details))

    def send_message(self, message: str) -> bool:
        if not self.is_configured:
            logger.warning(
                "Telegram alerts are disabled because TELEGRAM_BOT_TOKEN or "
                "TELEGRAM_CHAT_ID is not set."
            )
            return False

        payload = json.dumps(
            {
                "chat_id": self._config.chat_id,
                "text": message,
            }
        ).encode("utf-8")
        request = Request(
            self._api_url(),
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        self._sender(request, self._timeout_seconds)
        return True

    def _api_url(self) -> str:
        return f"https://api.telegram.org/bot{self._config.bot_token}/sendMessage"


def _clean_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _default_sender(request: Request, timeout_seconds: float) -> None:
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response.read()
    except HTTPError as exc:
        raise TelegramDeliveryError(f"Telegram API returned HTTP {exc.code}.") from exc
    except URLError as exc:
        raise TelegramDeliveryError(f"Telegram API request failed: {exc.reason}.") from exc


__all__ = [
    "TelegramAlertError",
    "TelegramAlerter",
    "TelegramConfig",
    "TelegramDeliveryError",
]
