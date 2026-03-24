from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest

from alerts.formatter import AlertType, MessageFormatter
from alerts.telegram import TelegramAlerter


@pytest.mark.parametrize(
    ("alert_type", "details", "expected_fragments"),
    [
        (
            AlertType.POSITION_OPENED,
            {
                "pair": "DOGE/USD",
                "side": "long",
                "entry_price": Decimal("0.1200"),
                "quantity": Decimal("250"),
                "stop_price": Decimal("0.1140"),
                "target_price": Decimal("0.1320"),
            },
            ("[POSITION OPENED]", "Pair: DOGE/USD", "Entry: 0.1200", "Quantity: 250"),
        ),
        (
            AlertType.POSITION_CLOSED,
            {
                "pair": "DOGE/USD",
                "side": "long",
                "exit_price": Decimal("0.1310"),
                "pnl_usd": Decimal("12.45"),
                "reason": "target_hit",
            },
            ("[POSITION CLOSED]", "Pair: DOGE/USD", "Exit: 0.1310", "PnL: 12.45"),
        ),
        (
            AlertType.STOP_TRIGGERED,
            {
                "pair": "DOGE/USD",
                "trigger_price": Decimal("0.1140"),
                "stop_price": Decimal("0.1150"),
                "position_id": "pos-123",
            },
            ("[STOP TRIGGERED]", "Pair: DOGE/USD", "Trigger: 0.1140", "Position ID: pos-123"),
        ),
        (
            AlertType.RECONCILIATION_MISMATCH,
            {
                "pair": "DOGE/USD",
                "severity": "high",
                "expected": "No open Kraken position",
                "actual": "Found long position",
                "summary": "Imported foreign position from exchange state.",
            },
            (
                "[RECONCILIATION MISMATCH]",
                "Pair: DOGE/USD",
                "Severity: high",
                "Summary: Imported foreign position from exchange state.",
            ),
        ),
        (
            AlertType.BELIEF_STALE,
            {
                "pair": "DOGE/USD",
                "belief_timestamp": datetime(2026, 3, 24, 11, 0, tzinfo=timezone.utc),
                "checked_at": datetime(2026, 3, 24, 16, 0, tzinfo=timezone.utc),
                "stale_after_hours": 4,
            },
            (
                "[BELIEF STALE]",
                "Pair: DOGE/USD",
                "Belief Time: 2026-03-24T11:00:00+00:00",
                "Max Age: 4",
            ),
        ),
        (
            AlertType.GUARDIAN_ACTION,
            {
                "pair": "DOGE/USD",
                "action_type": "limit_exit_attempt",
                "reason": "risk_violation",
                "trigger_price": Decimal("0.1140"),
                "message": "Reduce exposure immediately.",
            },
            (
                "[GUARDIAN ACTION]",
                "Pair: DOGE/USD",
                "Action: limit_exit_attempt",
                "Price: 0.1140",
            ),
        ),
    ],
)
def test_message_formatter_formats_all_supported_alert_types(
    alert_type: AlertType,
    details: dict[str, object],
    expected_fragments: tuple[str, ...],
) -> None:
    message = MessageFormatter().format(alert_type, details)

    for fragment in expected_fragments:
        assert fragment in message


def test_send_alert_posts_formatted_message_to_telegram() -> None:
    sender = Mock()
    alerter = TelegramAlerter(
        bot_token="bot-token",
        chat_id="123456",
        sender=sender,
    )

    sent = alerter.send_alert(
        AlertType.POSITION_CLOSED,
        {
            "pair": "DOGE/USD",
            "side": "long",
            "exit_price": Decimal("0.1310"),
            "pnl_usd": Decimal("12.45"),
            "reason": "target_hit",
        },
    )

    assert sent is True
    sender.assert_called_once()

    request, timeout_seconds = sender.call_args.args
    payload = json.loads(request.data.decode("utf-8"))

    assert request.full_url == "https://api.telegram.org/botbot-token/sendMessage"
    assert timeout_seconds == 10.0
    assert payload["chat_id"] == "123456"
    assert "[POSITION CLOSED]" in payload["text"]
    assert "Pair: DOGE/USD" in payload["text"]
    assert "PnL: 12.45" in payload["text"]


def test_send_alert_logs_warning_and_noops_without_telegram_config(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sender = Mock()
    alerter = TelegramAlerter(environ={}, sender=sender)

    with caplog.at_level("WARNING"):
        sent = alerter.send_alert(
            AlertType.POSITION_OPENED,
            {
                "pair": "DOGE/USD",
                "entry_price": Decimal("0.1200"),
            },
        )

    assert sent is False
    sender.assert_not_called()
    assert "TELEGRAM_BOT_TOKEN" in caplog.text
    assert "TELEGRAM_CHAT_ID" in caplog.text
