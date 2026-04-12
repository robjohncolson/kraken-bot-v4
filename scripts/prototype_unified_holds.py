"""Prototype: unified hold_scores via bidirectional pair analysis.

For every analyzed pair X/Y, compute two scores:
  - base_score  = score_entry(analysis)             → "how good is it to hold X"
  - quote_score = score_entry(invert_analysis(a))   → "how good is it to hold Y"

Aggregate per asset across all pairs touching it. USD, BTC, DOGE etc.
become directly comparable.
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cc_brain import (
    analyze_pair,
    score_entry,
    discover_all_pairs,
    _fetch_kraken_pairs,
)


def invert_analysis(a: dict) -> dict:
    """Mirror a pair analysis to the opposite direction.

    Flips direction-dependent signals so score_entry returns the "hold-quote"
    score instead of the "hold-base" score.
    """
    flipped = dict(a)
    if "/" in a.get("pair", ""):
        b, q = a["pair"].split("/", 1)
        flipped["pair"] = f"{q}/{b}"
    if a.get("price", 0) > 0:
        flipped["price"] = 1.0 / a["price"]

    trend_flip = {"UP": "DOWN", "DOWN": "UP", "UNKNOWN": "UNKNOWN"}
    flipped["trend_1h"] = trend_flip.get(a.get("trend_1h", "UNKNOWN"), "UNKNOWN")
    flipped["trend_4h"] = trend_flip.get(a.get("trend_4h", "UNKNOWN"), "UNKNOWN")

    # RSI: approximate inverse. Not mathematically exact but directionally right.
    flipped["rsi_1h"] = round(100.0 - float(a.get("rsi_1h", 50.0)), 1)

    dir_flip = {"bullish": "bearish", "bearish": "bullish", "neutral": "neutral"}
    flipped["kronos_direction"] = dir_flip.get(a.get("kronos_direction", "unknown"), "unknown")
    flipped["kronos_pct"] = -float(a.get("kronos_pct", 0))
    flipped["timesfm_direction"] = dir_flip.get(a.get("timesfm_direction", "unknown"), "unknown")

    # regime, trade_gate, kronos_volatility, timesfm_confidence — direction-agnostic
    return flipped


MIN_N_FOR_ELIGIBILITY = 3


def main() -> None:
    # Full cross-pair coverage: top USD-quoted + ALL BTC/ETH/USDT/USDC crosses.
    # This gives BTC/ETH/USDT/USDC the same statistical weight as USD.
    print("Discovering pairs...")
    usd_pairs = discover_all_pairs(limit=40)
    print(f"  {len(usd_pairs)} USD-quoted pairs (top by volume)")

    all_pairs = _fetch_kraken_pairs()

    def cross(quote: str) -> list[dict]:
        return [
            {"pair": n, "base": p["base"], "quote": p["quote"], "volume_usd": 0}
            for n, p in all_pairs.items() if p["quote"] == quote
        ]

    btc_cross = cross("BTC")
    eth_cross = cross("ETH")
    usdt_cross = cross("USDT")
    usdc_cross = cross("USDC")
    print(f"  {len(btc_cross)} BTC-quoted, {len(eth_cross)} ETH-quoted, "
          f"{len(usdt_cross)} USDT-quoted, {len(usdc_cross)} USDC-quoted")

    candidates = usd_pairs + btc_cross + eth_cross + usdt_cross + usdc_cross
    seen: set[str] = set()
    unique = []
    for c in candidates:
        if c["pair"] in seen:
            continue
        seen.add(c["pair"])
        unique.append(c)

    print(f"Analyzing {len(unique)} pairs (slow; uses Kronos+TimesFM)...")
    analyses: list[dict] = []
    for i, p in enumerate(unique, 1):
        print(f"  [{i:2d}/{len(unique)}] {p['pair']}", end=" ... ", flush=True)
        a = analyze_pair(p["pair"])
        if a is None:
            print("SKIP (no data)")
            continue
        base_s, _ = score_entry(a)
        quote_s, _ = score_entry(invert_analysis(a))
        # Skip pairs where both sides hit the regime floor — the pair is
        # untradeable in either direction and contributes no signal.
        if base_s == 0.0 and quote_s == 0.0:
            print("SKIP (regime gated)")
            continue
        analyses.append(a)
        print(f"base={base_s:.2f}  quote={quote_s:.2f}")

    # Aggregate per asset — keep only pairs where at least one side is alive.
    contributions: dict[str, list[tuple[str, float]]] = {}
    for a in analyses:
        if "/" not in a["pair"]:
            continue
        base, quote = a["pair"].split("/", 1)
        base_s, _ = score_entry(a)
        quote_s, _ = score_entry(invert_analysis(a))
        contributions.setdefault(base, []).append((a["pair"], base_s))
        contributions.setdefault(quote, []).append((a["pair"], quote_s))

    print(f"\n{len(analyses)} pairs survived regime filter")

    rows = []
    insufficient = []
    for asset, scores in contributions.items():
        values = sorted((s for _, s in scores), reverse=True)
        top3 = values[:3]
        row = {
            "asset": asset,
            "max": values[0],
            "top3_mean": sum(top3) / len(top3),
            "mean": sum(values) / len(values),
            "n": len(values),
        }
        if len(values) < MIN_N_FOR_ELIGIBILITY:
            insufficient.append(row)
        else:
            rows.append(row)

    # Primary aggregator: top-3 mean (dampens single-pair noise)
    rows.sort(key=lambda r: -r["top3_mean"])

    print(f"\n=== Eligible (n >= {MIN_N_FOR_ELIGIBILITY}), ranked by top-3 mean ===")
    print(f"{'asset':8s} {'top3m':>7s} {'max':>6s} {'mean':>6s} {'n':>4s}")
    print("-" * 40)
    for r in rows:
        print(f"{r['asset']:8s} {r['top3_mean']:7.3f} {r['max']:6.3f} {r['mean']:6.3f} {r['n']:4d}")

    print(f"\n=== Insufficient data (n < {MIN_N_FOR_ELIGIBILITY}), excluded ===")
    insufficient.sort(key=lambda r: -r["max"])
    for r in insufficient[:20]:
        print(f"{r['asset']:8s} {r['max']:6.3f} (n={r['n']})")

    # Spotlight the three the user mentioned + a few more key assets
    print("\n=== Focus: key assets ===")
    for target in ("USD", "BTC", "ETH", "USDT", "DOGE", "SOL"):
        if target not in contributions:
            print(f"\n{target}: no surviving pairs")
            continue
        items = sorted(contributions[target], key=lambda x: -x[1])
        values = [s for _, s in items]
        mx = values[0]
        top3 = sum(values[:3]) / min(3, len(values))
        print(f"\n{target}  max={mx:.3f}  top3m={top3:.3f}  n={len(values)}")
        for pair, score in items[:5]:
            print(f"  {pair:14s}  {score:.3f}")


if __name__ == "__main__":
    main()
