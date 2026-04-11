#!/usr/bin/env python3
"""CC Brain Loop — the trading intelligence.

This is the main decision-making script. Run every 1-2 hours (via cron or manual).
It orchestrates all CC tools: memory, regime detection, predictions, post-mortem,
and order placement into a single coherent decision cycle.

The Loop:
  1. Recall — read recent memories for context
  2. Observe — fetch portfolio state, market data, regime
  3. Analyze — RSI + EMA signals, Kronos predictions, HMM regime
  4. Post-mortem — analyze any newly closed trades
  5. Decide — for each position: hold/exit. For cash: enter or wait.
  6. Act — place orders via REST API
  7. Remember — write decisions, observations, snapshots to memory
  8. Report — generate human-readable summary

Usage:
    python scripts/cc_brain.py              # Full cycle
    python scripts/cc_brain.py --dry-run    # Analyze only, don't place orders
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

BOT_URL = "http://127.0.0.1:58392"
REVIEWS_DIR = Path(__file__).parent.parent / "state" / "cc-reviews"

# Strategy parameters
MAX_POSITION_USD = 10
MIN_REGIME_GATE = 0.40       # Don't enter if trade_gate below this
MIN_RSI_OVERSOLD = 35        # RSI below this = oversold (potential buy)
MAX_RSI_OVERBOUGHT = 70      # RSI above this = overbought (potential sell)
TARGET_MONTHLY_PCT = 1.0     # 1% monthly target
TOP_PAIRS = [
    "SOL/USD", "BTC/USD", "ETH/USD", "AVAX/USD", "LINK/USD",
    "AAVE/USD", "DOT/USD", "ATOM/USD", "ADA/USD", "MATIC/USD",
    "CRV/USD", "UNI/USD", "DOGE/USD", "NEAR/USD", "FTM/USD",
]


def fetch(endpoint: str, method: str = "GET", data: dict | None = None) -> dict:
    url = f"{BOT_URL}{endpoint}"
    if data:
        req = urllib.request.Request(
            url, data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"}, method=method,
        )
    else:
        req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        return {"error": str(exc)}


def compute_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = [max(0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def compute_ema(data: list[float], span: int) -> float:
    m = 2 / (span + 1)
    e = data[0]
    for v in data[1:]:
        e = v * m + e * (1 - m)
    return e


def analyze_pair(pair: str) -> dict | None:
    """Full analysis of a single pair: regime + RSI + EMA + Kronos."""
    # Regime
    regime_data = fetch(f"/api/regime/{pair.replace('/', '%2F')}?interval=60&count=300")
    if "error" in regime_data:
        return None

    # 1H bars for RSI + EMA
    ohlcv_1h = fetch(f"/api/ohlcv/{pair.replace('/', '%2F')}?interval=60&count=50")
    if "error" in ohlcv_1h or not ohlcv_1h.get("bars"):
        return None
    closes_1h = [float(b["close"]) for b in ohlcv_1h["bars"]]

    # 4H bars for trend
    ohlcv_4h = fetch(f"/api/ohlcv/{pair.replace('/', '%2F')}?interval=240&count=50")
    closes_4h = [float(b["close"]) for b in ohlcv_4h.get("bars", [])] if "error" not in ohlcv_4h else []

    # Kronos prediction
    kronos = fetch(f"/api/kronos/{pair.replace('/', '%2F')}?interval=60&pred_len=24")

    # Compute signals
    rsi_1h = compute_rsi(closes_1h)
    ema7_1h = compute_ema(closes_1h, 7) if len(closes_1h) >= 7 else closes_1h[-1]
    ema26_1h = compute_ema(closes_1h, 26) if len(closes_1h) >= 26 else closes_1h[-1]
    trend_1h = "UP" if ema7_1h > ema26_1h else "DOWN"

    ema7_4h = compute_ema(closes_4h, 7) if len(closes_4h) >= 7 else None
    ema26_4h = compute_ema(closes_4h, 26) if len(closes_4h) >= 26 else None
    trend_4h = "UP" if (ema7_4h and ema26_4h and ema7_4h > ema26_4h) else "DOWN" if ema7_4h else "UNKNOWN"

    return {
        "pair": pair,
        "price": closes_1h[-1],
        "regime": regime_data.get("regime", "unknown"),
        "trade_gate": regime_data.get("trade_gate", 0.5),
        "regime_probs": regime_data.get("probabilities", {}),
        "rsi_1h": round(rsi_1h, 1),
        "trend_1h": trend_1h,
        "trend_4h": trend_4h,
        "ema7_1h": round(ema7_1h, 4),
        "ema26_1h": round(ema26_1h, 4),
        "kronos_direction": kronos.get("direction", "unknown"),
        "kronos_pct": kronos.get("pct_change", 0),
        "kronos_volatility": kronos.get("volatility_pct", 0),
    }


def score_entry(analysis: dict) -> float:
    """Score a pair for entry: 0 = don't trade, 1 = strong signal."""
    score = 0.0

    # Gate: regime must be tradeable
    if analysis["trade_gate"] < MIN_REGIME_GATE:
        return 0.0

    # Gate: 4H trend must be UP for buys
    if analysis["trend_4h"] != "UP":
        return 0.0

    # RSI component: oversold in uptrend = dip-buy opportunity
    rsi = analysis["rsi_1h"]
    if rsi < MIN_RSI_OVERSOLD:
        score += 0.4  # Strong oversold signal
    elif rsi < 50:
        score += 0.2  # Moderate

    # Kronos component
    if analysis["kronos_direction"] == "bullish":
        score += 0.3
    elif analysis["kronos_direction"] == "neutral":
        score += 0.1

    # Regime component: trending is ideal
    if analysis["regime"] == "trending":
        score += 0.3
    elif analysis["regime"] == "volatile":
        score += 0.1

    return min(1.0, score)


def run_brain(dry_run: bool = False) -> str:
    """Execute one full CC brain cycle. Returns a summary report."""
    now = datetime.now(timezone.utc)
    log_lines: list[str] = []

    def log(msg: str) -> None:
        log_lines.append(msg)
        print(msg)

    log(f"=== CC Brain Cycle — {now.strftime('%Y-%m-%d %H:%M UTC')} ===")
    log(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")

    # === Step 1: Recall ===
    log("\n--- Step 1: Recall ---")
    memory = fetch("/api/memory?hours=48&limit=10")
    if "error" not in memory:
        recent = memory.get("memories", [])
        log(f"Recent memories: {len(recent)}")
        for m in recent[:3]:
            log(f"  [{m['category']}] {m.get('pair', '-')} — {json.dumps(m['content'])[:80]}")
    else:
        log(f"Memory unavailable: {memory.get('error', 'unknown')}")

    # === Step 2: Observe ===
    log("\n--- Step 2: Observe ---")
    balances = fetch("/api/balances")
    cash_usd = float(balances.get("cash_usd", 0))
    log(f"Cash: ${cash_usd:.2f}")

    tree = fetch("/api/rotation-tree")
    open_positions = [n for n in tree.get("nodes", []) if n.get("depth", 0) == 0 and n["status"] == "open"]
    log(f"Open positions: {len(open_positions)}")
    for pos in open_positions:
        log(f"  {pos['asset']:8s} qty={pos['quantity_total'][:8]:>8s} pnl={pos.get('realized_pnl', 'n/a')}")

    # === Step 3: Analyze ===
    log("\n--- Step 3: Analyze ---")
    analyses: list[dict] = []
    for pair in TOP_PAIRS[:10]:  # Limit to top 10 to keep cycle fast
        analysis = analyze_pair(pair)
        if analysis:
            analyses.append(analysis)
            regime_sym = {"trending": "T", "ranging": "R", "volatile": "V"}.get(analysis["regime"], "?")
            log(f"  {pair:10s} {regime_sym} gate={analysis['trade_gate']:.2f} RSI={analysis['rsi_1h']:5.1f} "
                f"4H={analysis['trend_4h']:4s} Kronos={analysis['kronos_direction']:8s} ({analysis['kronos_pct']:+.1f}%)")

    # === Step 4: Post-mortem ===
    log("\n--- Step 4: Post-mortem ---")
    outcomes = fetch("/api/trade-outcomes?lookback_days=7")
    if "error" not in outcomes:
        recent_trades = outcomes.get("outcomes", [])
        wins = sum(1 for t in recent_trades if float(t.get("net_pnl", 0)) > 0)
        total_pnl = sum(float(t.get("net_pnl", 0)) for t in recent_trades)
        log(f"Last 7 days: {len(recent_trades)} trades, {wins} wins, P&L=${total_pnl:.4f}")
    else:
        log("Trade outcomes unavailable")
        recent_trades = []

    # === Step 5: Decide ===
    log("\n--- Step 5: Decide ---")
    orders_to_place: list[dict] = []

    # Score all pairs for entry
    scored = [(a, score_entry(a)) for a in analyses]
    scored.sort(key=lambda x: -x[1])

    # Best entry candidate
    if scored and scored[0][1] > 0.5 and cash_usd >= MAX_POSITION_USD:
        best = scored[0][0]
        score = scored[0][1]
        log(f"ENTRY SIGNAL: {best['pair']} score={score:.2f} (RSI={best['rsi_1h']}, 4H={best['trend_4h']}, "
            f"Kronos={best['kronos_direction']}, regime={best['regime']})")

        qty = round(MAX_POSITION_USD / best["price"], 6)
        orders_to_place.append({
            "pair": best["pair"], "side": "buy", "order_type": "limit",
            "quantity": str(qty), "limit_price": str(round(best["price"], 4)),
        })
    else:
        top_reason = "no cash" if cash_usd < MAX_POSITION_USD else (
            f"best score={scored[0][1]:.2f}" if scored else "no data")
        log(f"NO ENTRY: {top_reason}. Sitting out this cycle.")

    # Check open positions for exit signals
    for pos in open_positions:
        asset = pos["asset"]
        pair = pos.get("entry_pair")
        if not pair or asset in ("USD", "USDT", "USDC", "GBP", "EUR"):
            continue
        # Find analysis for this pair
        pos_analysis = next((a for a in analyses if a["pair"] == pair), None)
        if not pos_analysis:
            pos_analysis = analyze_pair(pair)
        if pos_analysis and pos_analysis["trend_4h"] == "DOWN" and pos_analysis["rsi_1h"] > MAX_RSI_OVERBOUGHT:
            log(f"EXIT SIGNAL: {pair} — 4H down + RSI={pos_analysis['rsi_1h']} (overbought in downtrend)")
            # Note: exits are handled by bot's TP/SL monitoring, but CC can force-sell
            # For now, just log the recommendation

    # === Step 6: Act ===
    log("\n--- Step 6: Act ---")
    if dry_run:
        log("DRY RUN — no orders placed")
        for order in orders_to_place:
            log(f"  WOULD: {order['side']} {order['quantity']} {order['pair']} @ {order['limit_price']}")
    else:
        for order in orders_to_place:
            result = fetch("/api/orders", method="POST", data=order)
            if "error" in result:
                log(f"  FAILED: {order['pair']} ��� {result['error']}")
            else:
                log(f"  PLACED: {order['pair']} txid={result.get('txid', '?')}")

    # === Step 7: Remember ===
    log("\n--- Step 7: Remember ---")
    # Portfolio snapshot
    fetch("/api/memory", method="POST", data={
        "category": "portfolio_snapshot",
        "content": {"cash_usd": cash_usd, "open_positions": len(open_positions),
                     "total_trades_7d": len(recent_trades)},
        "importance": 0.3,
    })

    # Record regime observations
    for a in analyses[:5]:
        fetch("/api/memory", method="POST", data={
            "category": "regime", "pair": a["pair"],
            "content": {"regime": a["regime"], "trade_gate": a["trade_gate"],
                        "rsi": a["rsi_1h"], "trend_4h": a["trend_4h"]},
            "importance": 0.4,
        })

    # Record decisions
    if orders_to_place:
        for order in orders_to_place:
            best_a = next((a for a in analyses if a["pair"] == order["pair"]), {})
            fetch("/api/memory", method="POST", data={
                "category": "decision", "pair": order["pair"],
                "content": {"action": order["side"], "quantity": order["quantity"],
                            "price": order["limit_price"], "dry_run": dry_run,
                            "signals": {k: best_a.get(k) for k in ["rsi_1h", "trend_1h", "trend_4h",
                                                                     "kronos_direction", "regime", "trade_gate"]}},
                "importance": 0.8,
            })
    else:
        fetch("/api/memory", method="POST", data={
            "category": "decision",
            "content": {"action": "hold", "reason": top_reason if 'top_reason' in dir() else "no signal"},
            "importance": 0.5,
        })

    log(f"\nMemories written. Total: {fetch('/api/memory?hours=1&limit=100').get('count', '?')} this hour.")

    # === Step 8: Report ===
    report = "\n".join(log_lines)

    # Save report
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    ts = now.strftime("%Y-%m-%d_%H%M")
    report_path = REVIEWS_DIR / f"brain_{ts}.md"
    report_path.write_text(f"```\n{report}\n```\n", encoding="utf-8")
    print(f"\nReport saved to {report_path}")

    return report


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    run_brain(dry_run=dry_run)


if __name__ == "__main__":
    main()
