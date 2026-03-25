"""Pure parsing helpers for Kraken WebSocket v2 messages."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from exchange.symbols import SymbolNormalizationError, normalize_pair

UtcNow = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class PriceTick:
    pair: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class FillConfirmed:
    order_id: str
    client_order_id: str | None
    pair: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    timestamp: datetime


def decode_message(raw_message: str | bytes) -> dict[str, object] | None:
    text = raw_message.decode("utf-8") if isinstance(raw_message, bytes) else raw_message
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def is_ping_message(payload: dict[str, object]) -> bool:
    return any(payload.get(k) == "ping" for k in ("method", "event", "type", "channel"))


def build_pong_message(payload: dict[str, object]) -> dict[str, object]:
    for key in ("event", "type", "channel"):
        if payload.get(key) == "ping":
            pong: dict[str, object] = {key: "pong"}
            if "req_id" in payload:
                pong["req_id"] = payload["req_id"]
            return pong
    pong = {"method": "pong"}
    if "req_id" in payload:
        pong["req_id"] = payload["req_id"]
    return pong


def parse_ticker_payload(
    payload: Mapping[str, object], *, utc_now: UtcNow
) -> tuple[PriceTick, ...]:
    if payload.get("channel") != "ticker":
        return ()
    raw_data = payload.get("data")
    if not isinstance(raw_data, (list, tuple)):
        return ()
    msg_ts = _parse_timestamp(_first(payload, "timestamp", "time_in", "time_out"))
    ticks: list[PriceTick] = []
    for entry in raw_data:
        if not isinstance(entry, Mapping):
            continue
        raw_symbol = entry.get("symbol")
        if not isinstance(raw_symbol, str):
            continue
        try:
            pair = normalize_pair(raw_symbol)
        except SymbolNormalizationError:
            continue
        bid = _dec(_first(entry, "bid", "best_bid"))
        ask = _dec(_first(entry, "ask", "best_ask"))
        last = _dec(_first(entry, "last", "last_price"))
        if bid is None or ask is None or last is None:
            continue
        ts = _parse_timestamp(_first(entry, "timestamp", "time", "as_of")) or msg_ts or utc_now()
        ticks.append(PriceTick(pair=pair, bid=bid, ask=ask, last=last, timestamp=ts))
    return tuple(ticks)


def parse_execution_payload(
    payload: Mapping[str, object], *, utc_now: UtcNow
) -> tuple[FillConfirmed, ...]:
    if payload.get("channel") != "executions":
        return ()
    raw_data = payload.get("data")
    if not isinstance(raw_data, (list, tuple)):
        return ()
    msg_ts = _parse_timestamp(_first(payload, "timestamp", "time_in", "time_out"))
    fills: list[FillConfirmed] = []
    for entry in raw_data:
        if not isinstance(entry, Mapping) or entry.get("exec_type") != "trade":
            continue
        raw_oid = entry.get("order_id")
        raw_sym = entry.get("symbol")
        raw_side = entry.get("side")
        if not isinstance(raw_oid, str) or not raw_oid:
            continue
        if not isinstance(raw_sym, str) or not isinstance(raw_side, str):
            continue
        try:
            pair = normalize_pair(raw_sym)
        except SymbolNormalizationError:
            continue
        qty = _dec(_first(entry, "last_qty", "qty"))
        price = _dec(_first(entry, "last_price", "price"))
        fee = _extract_fee(entry)
        if qty is None or price is None or fee is None:
            continue
        cl_oid = entry.get("cl_ord_id")
        if not isinstance(cl_oid, str):
            cl_oid = None
        ts = _parse_timestamp(_first(entry, "timestamp", "time", "as_of")) or msg_ts or utc_now()
        fills.append(FillConfirmed(
            order_id=raw_oid, client_order_id=cl_oid, pair=pair,
            side=raw_side, quantity=qty, price=price, fee=fee, timestamp=ts,
        ))
    return tuple(fills)


# --- private helpers ---

def _first(mapping: Mapping[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _dec(value: object) -> Decimal | None:
    raw = value
    if isinstance(value, Mapping):
        raw = _first(value, "price", "value")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _extract_fee(entry: Mapping[str, object]) -> Decimal | None:
    raw_fees = entry.get("fees")
    if isinstance(raw_fees, (list, tuple)):
        total, found = Decimal("0"), False
        for fe in raw_fees:
            if isinstance(fe, Mapping):
                v = _dec(_first(fe, "qty", "fee"))
                if v is not None:
                    total += v
                    found = True
        if found:
            return total
    return _dec(_first(entry, "fee", "fee_usd_equiv"))


def _parse_timestamp(value: object) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return _to_utc(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _to_utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except ValueError:
        try:
            return datetime.fromtimestamp(float(value.strip()), tz=timezone.utc)
        except ValueError:
            return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
