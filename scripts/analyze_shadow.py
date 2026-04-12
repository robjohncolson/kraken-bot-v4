"""Analyze accumulated shadow_verdict memories from cc_brain cycles.

Reads shadow_verdict entries from the bot's memory API and summarizes:
  - How often live and shadow agree on "best hold"
  - Most common shadow picks vs most common live decisions
  - Disagreements: which held asset did shadow flag as weakest?
  - Eligibility stats (how often each asset has n >= 3)

Usage:
    python scripts/analyze_shadow.py [--hours N] [--limit N]

Default: last 168 hours (1 week), up to 200 entries.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.cc_brain import BOT_URL  # noqa: E402


def fetch_shadow_memories(hours: int, limit: int) -> list[dict]:
    url = f"{BOT_URL}/api/memory?category=shadow_verdict&hours={hours}&limit={limit}"
    with urllib.request.urlopen(url, timeout=15) as r:
        body = json.load(r)
    return body.get("memories", [])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    memories = fetch_shadow_memories(args.hours, args.limit)
    if not memories:
        print(f"No shadow_verdict memories found in last {args.hours}h.")
        print("Let the brain run a few cycles and try again.")
        return

    print(f"Found {len(memories)} shadow verdicts in last {args.hours}h\n")

    # Extract content
    verdicts: list[dict] = []
    for m in memories:
        c = m.get("content")
        if isinstance(c, str):
            try:
                c = json.loads(c)
            except json.JSONDecodeError:
                continue
        if isinstance(c, dict) and "best_shadow_hold" in c:
            verdicts.append(c)

    if not verdicts:
        print("No parseable shadow verdicts.")
        return

    # === Basic stats ===
    best_shadow_picks = Counter(v.get("best_shadow_hold") for v in verdicts)
    live_targets = Counter(
        v["live_decision"].get("pair", "-").split("/")[0] if v["live_decision"].get("type") == "order" else "HOLD"
        for v in verdicts
    )
    live_types = Counter(v["live_decision"].get("type", "unknown") for v in verdicts)

    print("=== Live decision types ===")
    for t, n in live_types.most_common():
        print(f"  {t:8s} {n:4d}  ({100*n/len(verdicts):.0f}%)")

    print("\n=== Most common shadow 'best hold' picks ===")
    for asset, n in best_shadow_picks.most_common(10):
        print(f"  {asset or '(none)':8s} {n:4d}  ({100*n/len(verdicts):.0f}%)")

    print("\n=== Most common live decision targets ===")
    for target, n in live_targets.most_common(10):
        print(f"  {target:8s} {n:4d}  ({100*n/len(verdicts):.0f}%)")

    # === Agreement / disagreement ===
    agreements = 0
    disagreements: list[tuple[str, str]] = []
    for v in verdicts:
        shadow = v.get("best_shadow_hold")
        live = v["live_decision"]
        if live.get("type") != "order":
            continue  # can't compare "hold" to a specific asset
        live_target = live.get("pair", "").split("/")[0]
        if shadow == live_target:
            agreements += 1
        else:
            disagreements.append((shadow or "(none)", live_target))

    total_comparable = agreements + len(disagreements)
    if total_comparable:
        rate = 100 * agreements / total_comparable
        print(f"\n=== Agreement on order cycles ===")
        print(f"  {agreements}/{total_comparable} ({rate:.0f}%) — shadow and live picked the same asset")
        if disagreements:
            print(f"\n  Top disagreement patterns (shadow -> live):")
            dis_count = Counter(disagreements)
            for (s, l), n in dis_count.most_common(10):
                print(f"    {s:8s} -> {l:8s}  {n:3d}x")

    # === Eligibility coverage ===
    asset_eligible_count: Counter = Counter()
    asset_total_count: Counter = Counter()
    for v in verdicts:
        eligible = v.get("eligible", {})
        for asset in eligible:
            asset_eligible_count[asset] += 1
        for asset in v.get("held_shadow", {}):
            asset_total_count[asset] += 1

    if asset_eligible_count:
        print("\n=== Eligibility: how often each asset had n >= 3 ===")
        for asset, n in asset_eligible_count.most_common(15):
            held_n = asset_total_count.get(asset, 0)
            hint = f" (held {held_n}x)" if held_n else ""
            print(f"  {asset:8s} {n:4d}/{len(verdicts):4d}  ({100*n/len(verdicts):.0f}%){hint}")

    # === Held-asset shadow distribution ===
    held_scores: dict[str, list[float]] = {}
    for v in verdicts:
        for asset, info in v.get("held_shadow", {}).items():
            if info.get("eligible") and info.get("top3_mean") is not None:
                held_scores.setdefault(asset, []).append(info["top3_mean"])

    if held_scores:
        print("\n=== Held-asset shadow top3_mean distribution ===")
        for asset, scores in sorted(held_scores.items(), key=lambda x: -sum(x[1])/len(x[1])):
            mean = sum(scores) / len(scores)
            print(f"  {asset:8s} avg={mean:.3f}  n={len(scores)}  range=[{min(scores):.3f}, {max(scores):.3f}]")


if __name__ == "__main__":
    main()
