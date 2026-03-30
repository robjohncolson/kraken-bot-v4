"""Evaluate shadow prediction logs against actual OHLCV outcomes.

Parses structured ``shadow_prediction`` log lines, fetches actual price
data from Kraken, and computes daily metrics for the shadow period.

Usage:
    python -m research.shadow_eval --log-file kraken-bot.log
    python -m research.shadow_eval --log-file kraken-bot.log --since 2026-03-29
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from research.ohlcv_history import fetch_ohlcv_history

# shadow_prediction: pair=DOGE/USD direction=bearish confidence=0.1064 prob_up=0.4468 artifact=...
SHADOW_RE = re.compile(
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"
    r".*shadow_prediction:\s+"
    r"pair=(?P<pair>\S+)\s+"
    r"direction=(?P<direction>\S+)\s+"
    r"confidence=(?P<confidence>[\d.]+)\s+"
    r"prob_up=(?P<prob_up>[\d.-]+)\s+"
    r"artifact=(?P<artifact>\S+)"
)

HORIZON_HOURS = 6
FEE_BPS = 10.0
SLIPPAGE_BPS = 5.0
COST_BPS = FEE_BPS + SLIPPAGE_BPS


def parse_shadow_log(log_path: Path, since: str | None = None) -> list[dict]:
    """Parse shadow_prediction lines from a log file."""
    predictions = []
    since_dt = datetime.fromisoformat(since) if since else None

    with open(log_path) as f:
        for line in f:
            m = SHADOW_RE.search(line)
            if not m:
                continue

            ts_str = m.group("timestamp")
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            if since_dt and ts < since_dt:
                continue

            prob_up = float(m.group("prob_up"))
            direction = m.group("direction")

            # Map direction to signal
            if direction == "bullish":
                signal = 1
            elif direction == "bearish":
                signal = -1
            else:
                signal = 0

            predictions.append({
                "timestamp": ts,
                "pair": m.group("pair"),
                "direction": direction,
                "signal": signal,
                "confidence": float(m.group("confidence")),
                "prob_up": prob_up,
                "artifact": m.group("artifact"),
            })

    return predictions


def fetch_outcomes(
    predictions: list[dict], pair: str,
) -> list[dict]:
    """Match predictions against actual OHLCV outcomes.

    Uses the paginated historical OHLCV fetcher (``fetch_ohlcv_history``)
    to cover the full shadow window.  The time range is derived from the
    earliest prediction minus a small buffer through the latest prediction
    plus the outcome horizon.  Predictions whose outcome window hasn't
    closed yet are excluded.
    """
    if not predictions:
        return []

    # Determine fetch window from prediction timestamps
    earliest_ts = min(int(p["timestamp"].timestamp()) for p in predictions)
    latest_ts = max(int(p["timestamp"].timestamp()) for p in predictions)
    since = (earliest_ts // 3600) * 3600 - 3600  # 1h buffer before earliest
    until = (latest_ts // 3600) * 3600 + (HORIZON_HOURS + 1) * 3600  # horizon + 1h after latest

    try:
        bars = fetch_ohlcv_history(pair, interval=60, since=since, until=until)
    except Exception as exc:
        print(f"WARNING: Could not fetch OHLCV for {pair}: {exc}", file=sys.stderr)
        return []

    # Build close lookup by hour-aligned timestamp
    close_by_ts: dict[int, float] = {}
    for _, row in bars.iterrows():
        close_by_ts[int(row["timestamp"])] = float(row["close"])

    results = []
    for pred in predictions:
        pred_ts = int(pred["timestamp"].timestamp())
        # Round to nearest hour
        pred_hour = (pred_ts // 3600) * 3600
        outcome_hour = pred_hour + HORIZON_HOURS * 3600

        close_at_pred = close_by_ts.get(pred_hour)
        close_at_outcome = close_by_ts.get(outcome_hour)

        if close_at_pred is None or close_at_outcome is None:
            continue  # outcome not yet available or data missing

        return_bps = 10000.0 * (close_at_outcome - close_at_pred) / close_at_pred
        actual_direction = 1 if return_bps > 0 else -1

        entry = dict(pred)
        entry["close_at_pred"] = close_at_pred
        entry["close_at_outcome"] = close_at_outcome
        entry["return_bps"] = return_bps
        entry["actual_direction"] = actual_direction

        # Paper P&L
        if pred["signal"] != 0:
            net_return = abs(return_bps) - COST_BPS
            entry["paper_pnl_bps"] = pred["signal"] * (return_bps - COST_BPS * (1 if return_bps > 0 else -1))
            # Simpler: signal * (return_bps) - cost always deducted
            entry["paper_pnl_bps"] = pred["signal"] * return_bps - COST_BPS
            entry["correct"] = (pred["signal"] == actual_direction)
        else:
            entry["paper_pnl_bps"] = 0.0
            entry["correct"] = None  # abstain

        results.append(entry)

    return results


def compute_metrics(results: list[dict], predictions: list[dict]) -> dict:
    """Compute aggregate metrics from matched prediction-outcome pairs."""
    total_predictions = len(predictions)
    matched = len(results)
    abstains = sum(1 for r in results if r["signal"] == 0)
    trades = [r for r in results if r["signal"] != 0]

    metrics: dict = {
        "total_predictions": total_predictions,
        "matched_outcomes": matched,
        "unmatched": total_predictions - matched,
        "prediction_coverage": matched / total_predictions if total_predictions else 0,
        "abstain_count": abstains,
        "abstain_rate": abstains / matched if matched else 0,
        "trade_count": len(trades),
    }

    if trades:
        correct = sum(1 for t in trades if t["correct"])
        pnls = [t["paper_pnl_bps"] for t in trades]
        metrics["directional_accuracy"] = correct / len(trades)
        metrics["hit_rate"] = sum(1 for p in pnls if p > 0) / len(trades)
        metrics["paper_pnl_bps"] = sum(pnls)
        metrics["mean_pnl_per_trade"] = sum(pnls) / len(trades)
    else:
        metrics["directional_accuracy"] = 0.0
        metrics["hit_rate"] = 0.0
        metrics["paper_pnl_bps"] = 0.0
        metrics["mean_pnl_per_trade"] = 0.0

    return metrics


def compute_daily_metrics(results: list[dict]) -> list[dict]:
    """Break down metrics by date."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        day = r["timestamp"].strftime("%Y-%m-%d")
        by_day[day].append(r)

    daily = []
    for day in sorted(by_day.keys()):
        day_results = by_day[day]
        trades = [r for r in day_results if r["signal"] != 0]
        correct = sum(1 for t in trades if t.get("correct"))
        pnls = [t["paper_pnl_bps"] for t in trades]

        daily.append({
            "date": day,
            "predictions": len(day_results),
            "trades": len(trades),
            "abstains": len(day_results) - len(trades),
            "accuracy": correct / len(trades) if trades else 0,
            "paper_pnl_bps": sum(pnls),
            "hit_rate": sum(1 for p in pnls if p > 0) / len(trades) if trades else 0,
        })
    return daily


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate shadow prediction logs against actual outcomes."
    )
    parser.add_argument("--log-file", required=True, type=str,
                        help="Path to the bot log file containing shadow_prediction lines")
    parser.add_argument("--since", type=str, default=None,
                        help="Only include predictions after this date (ISO format)")
    parser.add_argument("--pair", type=str, default="DOGE/USD",
                        help="Trading pair to evaluate (default: DOGE/USD)")
    args = parser.parse_args(argv)

    log_path = Path(args.log_file)
    if not log_path.exists():
        print(f"ERROR: Log file not found: {log_path}", file=sys.stderr)
        return 1

    print(f"Parsing shadow predictions from {log_path}...")
    predictions = parse_shadow_log(log_path, since=args.since)
    pair_preds = [p for p in predictions if p["pair"] == args.pair]
    print(f"Found {len(pair_preds)} shadow predictions for {args.pair}")

    if not pair_preds:
        print("No predictions to evaluate.")
        return 0

    print(f"Fetching OHLCV outcomes for {args.pair}...")
    results = fetch_outcomes(pair_preds, args.pair)
    print(f"Matched {len(results)} predictions to outcomes")

    # Aggregate metrics
    metrics = compute_metrics(results, pair_preds)
    print()
    print("=" * 60)
    print(f"SHADOW EVALUATION: {args.pair}")
    print("=" * 60)
    print(f"  Total predictions:     {metrics['total_predictions']}")
    print(f"  Matched to outcomes:   {metrics['matched_outcomes']}")
    print(f"  Prediction coverage:   {metrics['prediction_coverage']:.1%}")
    print(f"  Abstain rate:          {metrics['abstain_rate']:.1%}")
    print(f"  Trade count:           {metrics['trade_count']}")
    print(f"  Directional accuracy:  {metrics['directional_accuracy']:.1%}")
    print(f"  Hit rate:              {metrics['hit_rate']:.1%}")
    print(f"  Paper P&L:             {metrics['paper_pnl_bps']:+.2f} bps")
    print(f"  Mean P&L per trade:    {metrics['mean_pnl_per_trade']:+.2f} bps")

    # Daily breakdown
    daily = compute_daily_metrics(results)
    if daily:
        print()
        print(f"{'Date':<12} {'Preds':>6} {'Trades':>7} {'Abstain':>8} "
              f"{'Accuracy':>9} {'P&L(bps)':>10} {'HitRate':>8}")
        print("-" * 65)
        for d in daily:
            print(f"{d['date']:<12} {d['predictions']:>6} {d['trades']:>7} "
                  f"{d['abstains']:>8} {d['accuracy']:>9.1%} "
                  f"{d['paper_pnl_bps']:>+10.2f} {d['hit_rate']:>8.1%}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
