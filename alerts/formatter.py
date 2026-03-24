from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class AlertFormattingError(ValueError):
    """Base exception for alert message formatting failures."""


class UnsupportedAlertTypeError(AlertFormattingError):
    """Raised when an alert type does not have a formatter."""

    def __init__(self, alert_type: object) -> None:
        self.alert_type = alert_type
        super().__init__(f"Unsupported alert type {alert_type!r}.")


class AlertType(StrEnum):
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    STOP_TRIGGERED = "stop_triggered"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    BELIEF_STALE = "belief_stale"
    GUARDIAN_ACTION = "guardian_action"


class MessageFormatter:
    """Render structured alert payloads into Telegram-friendly text."""

    def format(
        self,
        alert_type: str | AlertType,
        details: Mapping[str, object],
    ) -> str:
        resolved_type = _coerce_alert_type(alert_type)
        if resolved_type is AlertType.POSITION_OPENED:
            return "\n".join(
                (
                    "[POSITION OPENED]",
                    f"Pair: {_value(details, 'pair')}",
                    f"Side: {_value(details, 'side')}",
                    f"Entry: {_first_value(details, 'entry_price', 'price')}",
                    f"Quantity: {_value(details, 'quantity')}",
                    f"Stop: {_value(details, 'stop_price')}",
                    f"Target: {_value(details, 'target_price')}",
                )
            )

        if resolved_type is AlertType.POSITION_CLOSED:
            return "\n".join(
                (
                    "[POSITION CLOSED]",
                    f"Pair: {_value(details, 'pair')}",
                    f"Side: {_value(details, 'side')}",
                    f"Exit: {_first_value(details, 'exit_price', 'price')}",
                    f"PnL: {_first_value(details, 'pnl_usd', 'pnl')}",
                    f"Reason: {_value(details, 'reason')}",
                )
            )

        if resolved_type is AlertType.STOP_TRIGGERED:
            return "\n".join(
                (
                    "[STOP TRIGGERED]",
                    f"Pair: {_value(details, 'pair')}",
                    f"Trigger: {_first_value(details, 'trigger_price', 'price')}",
                    f"Stop: {_value(details, 'stop_price')}",
                    f"Position ID: {_value(details, 'position_id')}",
                )
            )

        if resolved_type is AlertType.RECONCILIATION_MISMATCH:
            return "\n".join(
                (
                    "[RECONCILIATION MISMATCH]",
                    f"Pair: {_value(details, 'pair')}",
                    f"Severity: {_value(details, 'severity')}",
                    f"Expected: {_value(details, 'expected')}",
                    f"Actual: {_value(details, 'actual')}",
                    f"Summary: {_first_value(details, 'summary', 'message')}",
                )
            )

        if resolved_type is AlertType.BELIEF_STALE:
            return "\n".join(
                (
                    "[BELIEF STALE]",
                    f"Pair: {_value(details, 'pair')}",
                    f"Belief Time: {_value(details, 'belief_timestamp')}",
                    f"Checked At: {_value(details, 'checked_at')}",
                    f"Max Age: {_first_value(details, 'stale_after_hours', 'max_age_hours')}",
                )
            )

        if resolved_type is AlertType.GUARDIAN_ACTION:
            return "\n".join(
                (
                    "[GUARDIAN ACTION]",
                    f"Pair: {_value(details, 'pair')}",
                    f"Action: {_first_value(details, 'action_type', 'action')}",
                    f"Reason: {_first_value(details, 'reason', 'recommended_action')}",
                    f"Price: {_first_value(details, 'trigger_price', 'price')}",
                    f"Details: {_first_value(details, 'message', 'violation_type')}",
                )
            )

        raise UnsupportedAlertTypeError(alert_type)


def _coerce_alert_type(alert_type: str | AlertType) -> AlertType:
    try:
        return AlertType(str(alert_type))
    except ValueError as exc:
        raise UnsupportedAlertTypeError(alert_type) from exc


def _first_value(details: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        if key in details and details[key] is not None:
            return _render_value(details[key])
    return "n/a"


def _value(details: Mapping[str, object], key: str) -> str:
    return _first_value(details, key)


def _render_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


__all__ = [
    "AlertFormattingError",
    "AlertType",
    "MessageFormatter",
    "UnsupportedAlertTypeError",
]
