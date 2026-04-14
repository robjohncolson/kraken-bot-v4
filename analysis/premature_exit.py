"""Premature-exit detector (Qullamaggie rule).

Scans closed trades in trade_outcomes, reconstructs 4H EMA(10)/EMA(20)
at exit time from CryptoCompare historical bars, and tags exits where
price was still above both EMAs as ``premature_exit`` memories.

Detection only. This module does not change live exit logic.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))

import pandas as pd

from persistence.cc_memory import CCMemory
from research.ohlcv_cryptocompare import (
    CryptoCompareError,
    fetch_ohlcv_cryptocompare,
)

logger = logging.getLogger(__name__)

MIN_4H_BARS_REQUIRED = 20
EXCLUDED_REASONS = frozenset({"stop_loss"})
RULE_VERSION = "v1"


def _compute_ema(values: list[float], span: int) -> float:
    """Iterative EMA identical to ``scripts/cc_brain.compute_ema``."""
    m = 2 / (span + 1)
    e = values[0]
    for value in values[1:]:
        e = value * m + e * (1 - m)
    return e


def _classify(
    exit_price: Decimal,
    ema10: Decimal,
    ema20: Decimal,
    exit_reason: str,
) -> bool:
    """Return True iff the exit meets the v1 premature-exit rule."""
    if exit_reason in EXCLUDED_REASONS:
        return False
    return exit_price > ema10 and exit_price > ema20


def _aggregate_1h_to_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 1H bars into aligned 4H buckets and drop partial buckets."""
    columns = ["bucket_start", "open", "high", "low", "close", "volume"]
    if df_1h.empty:
        return pd.DataFrame(columns=columns)

    df = df_1h.copy()
    df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df.set_index("dt").sort_index()

    resampled = df.resample("4h", origin="epoch").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        _n=("open", "size"),
    )
    complete = resampled[resampled["_n"] == 4].drop(columns=["_n"])
    if complete.empty:
        return pd.DataFrame(columns=columns)

    complete = complete.reset_index().rename(columns={"dt": "bucket_start"})
    return complete[columns]


def _fetch_trade_outcomes(bot_url: str, lookback_days: int) -> list[dict]:
    """Fetch closed trade outcomes from the bot REST API."""
    url = f"{bot_url.rstrip('/')}/api/trade-outcomes?lookback_days={lookback_days}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return data.get("outcomes", [])


def _already_flagged_ids(cc_memory: CCMemory) -> set[int]:
    """Collect trade_outcome ids already recorded as premature exits."""
    rows = cc_memory.query(
        category="premature_exit",
        hours=24 * 365,
        limit=10000,
    )
    flagged_ids: set[int] = set()
    for row in rows:
        content = row.get("content") or {}
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                continue
        try:
            outcome_id = content.get("trade_outcome_id")
            if outcome_id is not None:
                flagged_ids.add(int(outcome_id))
        except (AttributeError, TypeError, ValueError):
            continue
    return flagged_ids


def detect_premature_exits(
    lookback_days: int,
    cc_memory: CCMemory,
    trade_outcomes: list[dict],
    *,
    dry_run: bool = False,
    fetch_bars: Callable[..., pd.DataFrame] = fetch_ohlcv_cryptocompare,
) -> dict:
    """Detect premature exits and optionally write them to ``cc_memory``."""
    result = {
        "scanned": 0,
        "flagged": 0,
        "skipped": 0,
        "errors": 0,
    }
    already_flagged = _already_flagged_ids(cc_memory)

    for outcome in trade_outcomes:
        result["scanned"] += 1

        try:
            outcome_id = int(outcome["id"])
            pair = str(outcome["pair"])
            exit_reason = str(outcome.get("exit_reason", ""))
            closed_at = str(outcome["closed_at"])
            exit_price = Decimal(str(outcome["exit_price"]))
            net_pnl = Decimal(str(outcome.get("net_pnl", "0")))
            exit_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
            if exit_dt.tzinfo is None:
                exit_dt = exit_dt.replace(tzinfo=timezone.utc)
            else:
                exit_dt = exit_dt.astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            logger.warning("Skipping malformed trade outcome %r: %s", outcome, exc)
            result["errors"] += 1
            continue

        if outcome_id in already_flagged:
            result["skipped"] += 1
            continue

        exit_ts = int(exit_dt.timestamp())
        since_ts = exit_ts - (lookback_days * 86400)

        try:
            bars_1h = fetch_bars(
                pair,
                interval=60,
                since=since_ts,
                until=exit_ts,
            )
        except CryptoCompareError as exc:
            logger.warning(
                "Premature-exit detector skipping %s (%s): %s",
                outcome_id,
                pair,
                exc,
            )
            result["errors"] += 1
            continue
        except Exception as exc:
            logger.warning(
                "Premature-exit detector unexpected error for %s (%s): %s",
                outcome_id,
                pair,
                exc,
            )
            result["errors"] += 1
            continue

        bars_4h = _aggregate_1h_to_4h(bars_1h)
        if not bars_4h.empty:
            cutoff = pd.Timestamp(exit_dt)
            bars_4h = bars_4h[
                (bars_4h["bucket_start"] + pd.Timedelta(hours=4)) <= cutoff
            ].copy()

        if len(bars_4h) < MIN_4H_BARS_REQUIRED:
            result["skipped"] += 1
            continue

        closes = [float(value) for value in bars_4h["close"].tolist()]
        ema10 = Decimal(str(_compute_ema(closes, 10)))
        ema20 = Decimal(str(_compute_ema(closes, 20)))

        if not _classify(exit_price, ema10, ema20, exit_reason):
            continue

        result["flagged"] += 1
        payload = {
            "trade_outcome_id": outcome_id,
            "closed_at": closed_at,
            "exit_reason": exit_reason,
            "exit_price": str(exit_price),
            "ema10_4h": str(ema10),
            "ema20_4h": str(ema20),
            "net_pnl": str(net_pnl),
            "rule_version": RULE_VERSION,
        }

        if dry_run:
            logger.info(
                "Premature exit detected (dry-run) for %s (%s): %s",
                outcome_id,
                pair,
                payload,
            )
            already_flagged.add(outcome_id)
            continue

        row_id = cc_memory._write(
            "premature_exit",
            payload,
            pair=pair,
            importance=0.7,
        )
        if row_id:
            already_flagged.add(outcome_id)
        else:
            result["errors"] += 1

    return result


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Premature exit detector")
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--bot-url", default="http://127.0.0.1:58392")
    parser.add_argument("--db-path", default="data/bot.db")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    outcomes = _fetch_trade_outcomes(args.bot_url, args.lookback_days)
    cc_memory = CCMemory(args.db_path)
    result = detect_premature_exits(
        lookback_days=args.lookback_days,
        cc_memory=cc_memory,
        trade_outcomes=outcomes,
        dry_run=args.dry_run,
    )
    print(
        f"[premature_exit] scanned={result['scanned']} "
        f"flagged={result['flagged']} skipped={result['skipped']} "
        f"errors={result['errors']} dry_run={args.dry_run}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

