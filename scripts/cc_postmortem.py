#!/usr/bin/env python3
"""CC Post-Mortem Engine — analyzes closed trades and generates improvement insights.

Run standalone:  python scripts/cc_postmortem.py
Or via REST:     Called by CC during scheduled review loops.

Reads trade_outcomes from the bot's REST API, computes performance metrics,
identifies patterns in wins/losses, and writes a markdown report to
state/cc-reviews/postmortem_YYYY-MM-DD_HHMM.md
"""
from __future__ import annotations

import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

BOT_URL = "http://127.0.0.1:58392"
REVIEWS_DIR = Path(__file__).parent.parent / "state" / "cc-reviews"


def fetch_json(endpoint: str) -> dict:
    url = f"{BOT_URL}{endpoint}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def run_postmortem(lookback_days: int = 30) -> str:
    """Run full post-mortem analysis. Returns the markdown report."""
    data = fetch_json(f"/api/trade-outcomes?lookback_days={lookback_days}")
    outcomes = data.get("outcomes", [])

    if not outcomes:
        return "# Post-Mortem\n\nNo trades in the last {lookback_days} days.\n"

    # Parse into structured data
    trades = []
    for o in outcomes:
        trades.append({
            "id": o["id"],
            "pair": o["pair"],
            "direction": o["direction"],
            "entry_price": Decimal(str(o["entry_price"])),
            "exit_price": Decimal(str(o["exit_price"])),
            "entry_cost": Decimal(str(o["entry_cost"])),
            "exit_proceeds": Decimal(str(o["exit_proceeds"])),
            "net_pnl": Decimal(str(o["net_pnl"])),
            "fee_total": Decimal(str(o["fee_total"])),
            "exit_reason": o["exit_reason"],
            "hold_hours": float(o["hold_hours"]) if o["hold_hours"] else 0,
            "confidence": float(o["confidence"]) if o["confidence"] else 0,
            "node_depth": o["node_depth"],
            "opened_at": o["opened_at"],
            "closed_at": o["closed_at"],
        })

    # === Aggregate Metrics ===
    total = len(trades)
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = win_count / total * 100 if total > 0 else 0

    total_pnl = sum(t["net_pnl"] for t in trades)
    total_fees = sum(t["fee_total"] for t in trades)
    avg_win = sum(t["net_pnl"] for t in wins) / win_count if wins else Decimal(0)
    avg_loss = sum(t["net_pnl"] for t in losses) / loss_count if losses else Decimal(0)
    gross_wins = sum(t["net_pnl"] for t in wins)
    gross_losses = abs(sum(t["net_pnl"] for t in losses))
    profit_factor = float(gross_wins / gross_losses) if gross_losses > 0 else float("inf")

    avg_hold_hours = sum(t["hold_hours"] for t in trades) / total
    avg_hold_wins = sum(t["hold_hours"] for t in wins) / win_count if wins else 0
    avg_hold_losses = sum(t["hold_hours"] for t in losses) / loss_count if losses else 0

    # === By Pair ===
    by_pair: dict[str, list] = defaultdict(list)
    for t in trades:
        by_pair[t["pair"]].append(t)

    # === By Exit Reason ===
    by_reason: dict[str, list] = defaultdict(list)
    for t in trades:
        by_reason[t["exit_reason"]].append(t)

    # === By Depth (root vs child) ===
    roots = [t for t in trades if t["node_depth"] == 0]
    children = [t for t in trades if t["node_depth"] > 0]

    # === Pattern Detection ===
    patterns = []

    # Pattern: trades that hit SL very quickly (< 1h) — likely bad entries
    quick_sl = [t for t in trades if t["exit_reason"] == "stop_loss" and t["hold_hours"] < 1.0]
    if quick_sl:
        pairs = ", ".join(set(t["pair"] for t in quick_sl))
        patterns.append(
            f"**Quick Stop-Losses ({len(quick_sl)} trades < 1h)**: {pairs}. "
            f"These entries were immediately wrong — consider tighter entry criteria "
            f"(higher RSI threshold, stronger 4H alignment, or 15M confirmation)."
        )

    # Pattern: same pair losing repeatedly
    for pair, pair_trades in by_pair.items():
        pair_losses = [t for t in pair_trades if t["net_pnl"] <= 0]
        if len(pair_losses) >= 3:
            patterns.append(
                f"**Repeat Loser: {pair}** — {len(pair_losses)}/{len(pair_trades)} trades lost. "
                f"Consider adding to cooldown/blocklist."
            )

    # Pattern: wins are smaller than losses (bad R:R)
    if wins and losses:
        rr_ratio = float(avg_win / abs(avg_loss)) if avg_loss != 0 else float("inf")
        if rr_ratio < 1.5:
            patterns.append(
                f"**Poor R:R Ratio ({rr_ratio:.2f}:1)**: Average win (${avg_win:.4f}) is less than "
                f"1.5x average loss (${abs(avg_loss):.4f}). Consider widening TP or tightening SL."
            )

    # Pattern: high-confidence trades losing
    high_conf_losses = [t for t in losses if t["confidence"] >= 0.8]
    if high_conf_losses:
        patterns.append(
            f"**High-Confidence Losses ({len(high_conf_losses)} trades, conf >= 0.8)**: "
            f"The signal was confident but wrong. Check if 4H trend was aligned at entry time."
        )

    # Pattern: timer exits (held to deadline without hitting TP or SL)
    timer_exits = [t for t in trades if t["exit_reason"] == "timer"]
    if timer_exits:
        timer_wins = [t for t in timer_exits if t["net_pnl"] > 0]
        patterns.append(
            f"**Timer Exits ({len(timer_exits)} trades)**: {len(timer_wins)} profitable, "
            f"{len(timer_exits) - len(timer_wins)} losses. "
            f"{'Consider extending deadlines.' if len(timer_wins) / len(timer_exits) > 0.6 else 'Deadlines are appropriate.'}"
        )

    # Pattern: fees eating profits
    fee_ratio = float(total_fees / gross_wins) * 100 if gross_wins > 0 else 100
    if fee_ratio > 30:
        patterns.append(
            f"**High Fee Burden ({fee_ratio:.0f}% of gross wins)**: Fees are ${total_fees:.4f} vs "
            f"gross wins ${gross_wins:.4f}. Need larger wins or fewer trades."
        )

    # === Build Report ===
    now = datetime.now(timezone.utc)
    report = f"""# CC Post-Mortem — {now.strftime('%Y-%m-%d %H:%M UTC')}
## Lookback: {lookback_days} days | Trades: {total}

### Summary
| Metric | Value |
|--------|-------|
| Win Rate | {win_rate:.0f}% ({win_count}W / {loss_count}L) |
| Total P&L | ${total_pnl:.4f} |
| Total Fees | ${total_fees:.4f} |
| Avg Win | ${avg_win:.4f} |
| Avg Loss | ${avg_loss:.4f} |
| Profit Factor | {profit_factor:.2f} |
| Avg Hold (all) | {avg_hold_hours:.1f}h |
| Avg Hold (wins) | {avg_hold_wins:.1f}h |
| Avg Hold (losses) | {avg_hold_losses:.1f}h |

### By Pair
| Pair | Trades | Wins | Losses | Net P&L | Avg P&L |
|------|--------|------|--------|---------|---------|
"""
    for pair in sorted(by_pair.keys()):
        pt = by_pair[pair]
        pw = [t for t in pt if t["net_pnl"] > 0]
        net = sum(t["net_pnl"] for t in pt)
        avg = net / len(pt)
        report += f"| {pair} | {len(pt)} | {len(pw)} | {len(pt) - len(pw)} | ${net:.4f} | ${avg:.4f} |\n"

    report += "\n### By Exit Reason\n"
    report += "| Reason | Count | Avg P&L | Avg Hold |\n"
    report += "|--------|-------|---------|----------|\n"
    for reason in sorted(by_reason.keys()):
        rt = by_reason[reason]
        avg_pnl = sum(t["net_pnl"] for t in rt) / len(rt)
        avg_h = sum(t["hold_hours"] for t in rt) / len(rt)
        report += f"| {reason} | {len(rt)} | ${avg_pnl:.4f} | {avg_h:.1f}h |\n"

    if roots or children:
        report += "\n### Root vs Child Trades\n"
        report += "| Type | Count | Net P&L | Win Rate |\n"
        report += "|------|-------|---------|----------|\n"
        if roots:
            rw = [t for t in roots if t["net_pnl"] > 0]
            report += f"| Root (depth=0) | {len(roots)} | ${sum(t['net_pnl'] for t in roots):.4f} | {len(rw)/len(roots)*100:.0f}% |\n"
        if children:
            cw = [t for t in children if t["net_pnl"] > 0]
            report += f"| Child (depth>0) | {len(children)} | ${sum(t['net_pnl'] for t in children):.4f} | {len(cw)/len(children)*100:.0f}% |\n"

    if patterns:
        report += "\n### Patterns & Recommendations\n"
        for i, p in enumerate(patterns, 1):
            report += f"\n{i}. {p}\n"
    else:
        report += "\n### Patterns\n\nNo actionable patterns detected yet (need more trade data).\n"

    # === Parameter Suggestions ===
    report += "\n### Suggested Parameter Adjustments\n"
    suggestions = []

    if win_rate < 50 and total >= 5:
        suggestions.append("- **Raise entry bar**: Increase ROTATION_MIN_CONFIDENCE from 0.65 to 0.75")
    if profit_factor < 1.5 and total >= 5:
        suggestions.append("- **Improve R:R**: Widen ROTATION_TAKE_PROFIT_PCT or tighten ROTATION_STOP_LOSS_PCT")
    if quick_sl and len(quick_sl) / total > 0.3:
        suggestions.append("- **Entry timing**: Enable/tune MTF_15M_CONFIRM to avoid entering at bad moments")
    if fee_ratio > 30:
        suggestions.append("- **Reduce churn**: Increase MIN_POSITION_USD to make each trade more meaningful relative to fees")
    if not suggestions:
        suggestions.append("- No changes recommended yet — need more data points")

    for s in suggestions:
        report += f"{s}\n"

    report += f"\n---\n*Generated by CC Post-Mortem Engine at {now.isoformat()}*\n"
    return report


def main() -> None:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    report = run_postmortem(lookback_days=30)

    # Write to file
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = REVIEWS_DIR / f"postmortem_{ts}.md"
    path.write_text(report, encoding="utf-8")
    print(f"Post-mortem written to {path}")

    # Also print to stdout
    print(report)

    # Premature-exit detector (best-effort, non-fatal)
    try:
        from analysis.premature_exit import detect_premature_exits
        from persistence.cc_memory import CCMemory

        outcomes = fetch_json("/api/trade-outcomes?lookback_days=30").get("outcomes", [])
        db_path = Path(__file__).resolve().parents[1] / "data" / "bot.db"
        result = detect_premature_exits(
            lookback_days=30,
            cc_memory=CCMemory(str(db_path)),
            trade_outcomes=outcomes,
        )
        print(
            f"[premature_exit] scanned={result['scanned']} "
            f"flagged={result['flagged']} skipped={result['skipped']} "
            f"errors={result['errors']}"
        )
    except Exception as exc:
        print(f"[premature_exit] detector error (non-fatal): {exc}")


if __name__ == "__main__":
    main()
