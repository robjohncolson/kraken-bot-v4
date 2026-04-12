"""Historical backfill: replay compute_unified_holds against every past
brain_*.md report and compare its verdict to what the bot actually did.

Strategy
--------
Brain reports at state/cc-reviews/brain_*.md preserve enough of each pair
analysis (regime, trade_gate, RSI, trend_4h, Kronos direction, TimesFM
direction, and the full score_entry breakdown) to reconstruct analysis
dicts that are equivalent for scoring purposes. We recover trend_1h and
timesfm_confidence from the breakdown values (reverse-solving
score_entry's arithmetic).

Then for each historical cycle we:
  1) Re-run compute_unified_holds(analyses) to get the shadow verdict.
  2) Parse the live decision from the "Step 5: Decide" block.
  3) Compare. Tally agreement, top disagreements, eligibility coverage.

Usage:
    python scripts/backfill_shadow.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.cc_brain import _fetch_kraken_pairs, compute_unified_holds, score_entry  # noqa: E402

_REPORTS_DIR = Path(__file__).resolve().parent.parent / "state" / "cc-reviews"
_KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"

# Regex: parses one analyzed-pair line from the Step 3 Analyze section.
# Example:
#   BTC/USD    V gate=1.00 RSI= 36.3 4H=UP   K=bull TFM=neut => 0.70 [4H_trend=+0.20 1H_trend=+0.00 RSI=+0.20 Kronos=+0.20 TimesFM=+0.00 regime=+0.10]
_ANALYSIS_LINE = re.compile(
    r"^\s{2,}(\S+)\s+([TVR?])\s+gate=([\d.]+)\s+RSI=\s*([\d.]+)\s+"
    r"4H=(\S+)\s+K=(\w+)\s+TFM=(\w+)\s+=>\s+([\d.]+)\s+"
    r"\[(.*?)\]"
)

# Regex: parses the live entry/rotation/exit line from Step 5.
_ENTRY_LINE = re.compile(r"ENTRY\s+from\s+\S+:\s+(\S+)\s+score=([\d.]+)")
_ROTATION_LINE = re.compile(r"ROTATION:\s+(\S+)\s+->\s+(\S+)\s+via\s+(\S+)")
_EXIT_LINE = re.compile(r"EXIT:\s+(\S+)\s+via\s+(\S+)")

_REGIME_MAP = {"T": "trending", "V": "volatile", "R": "ranging", "?": "unknown"}
_DIR_MAP = {"bull": "bullish", "bear": "bearish", "neut": "neutral", "unkn": "unknown"}


def _parse_breakdown(bd_str: str) -> dict[str, float]:
    """Parse a breakdown string like '4H_trend=+0.20 RSI=+0.20 TimesFM=+0.00'."""
    bd: dict[str, float] = {}
    for tok in bd_str.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            try:
                bd[k] = float(v)
            except ValueError:
                pass
    return bd


def _reconstruct_analysis(m: re.Match) -> dict:
    """Build a partial analysis dict from one parsed report line.

    Fields populated are exactly the ones score_entry / invert_analysis
    read. Others (price, ema, regime_probs, kronos_pct, volatility) are
    omitted — they don't affect scoring.
    """
    pair = m.group(1)
    regime = _REGIME_MAP.get(m.group(2), "unknown")
    gate = float(m.group(3))
    rsi = float(m.group(4))
    trend_4h = m.group(5) if m.group(5) in ("UP", "DOWN") else "UNKNOWN"
    kdir = _DIR_MAP.get(m.group(6), "unknown")
    tdir = _DIR_MAP.get(m.group(7), "unknown")
    bd = _parse_breakdown(m.group(9))

    # Recover trend_1h from breakdown: +0.10 -> UP, else UNKNOWN (score_entry
    # treats DOWN and UNKNOWN identically so we can't distinguish them, but
    # that's fine — score_entry doesn't either).
    trend_1h = "UP" if bd.get("1H_trend", 0.0) >= 0.09 else "UNKNOWN"

    # Recover timesfm_confidence from the TimesFM contribution:
    # bullish => conf = contrib / 0.20
    # bearish => conf = -contrib / 0.10
    tfm_contrib = bd.get("TimesFM", 0.0)
    if tdir == "bullish" and tfm_contrib > 0:
        tfm_conf = min(1.0, tfm_contrib / 0.20)
    elif tdir == "bearish" and tfm_contrib < 0:
        tfm_conf = min(1.0, -tfm_contrib / 0.10)
    else:
        tfm_conf = 0.0

    return {
        "pair": pair,
        "price": 1.0,  # placeholder, not used by score_entry
        "regime": regime,
        "trade_gate": gate,
        "regime_probs": {},
        "rsi_1h": rsi,
        "trend_1h": trend_1h,
        "trend_4h": trend_4h,
        "ema7_1h": 0,
        "ema26_1h": 0,
        "kronos_direction": kdir,
        "kronos_pct": 0,
        "kronos_volatility": 0,
        "timesfm_direction": tdir,
        "timesfm_confidence": tfm_conf,
    }


def _parse_report(path: Path) -> tuple[list[dict], dict]:
    """Return (analyses, live_decision) for a single report file."""
    text = path.read_text(encoding="utf-8", errors="replace")

    # Isolate the Step 3 Analyze section
    step3_m = re.search(r"--- Step 3: Analyze ---\n(.*?)(?=\n--- Step 4)", text, re.DOTALL)
    analyses: list[dict] = []
    if step3_m:
        for line in step3_m.group(1).splitlines():
            am = _ANALYSIS_LINE.match(line)
            if am:
                analyses.append(_reconstruct_analysis(am))

    # Parse Step 5 decision
    step5_m = re.search(r"--- Step 5: Decide ---\n(.*?)(?=\n--- Step 6)", text, re.DOTALL)
    live: dict = {"type": "hold"}
    if step5_m:
        block = step5_m.group(1)
        em = _ENTRY_LINE.search(block)
        rm = _ROTATION_LINE.search(block)
        xm = _EXIT_LINE.search(block)
        if em:
            live = {"type": "entry", "pair": em.group(1), "score": float(em.group(2)),
                    "asset": em.group(1).split("/")[0]}
        elif rm:
            live = {"type": "rotation", "from": rm.group(1), "to": rm.group(2),
                    "pair": rm.group(3), "asset": rm.group(2)}
        elif xm:
            live = {"type": "exit", "asset": xm.group(1), "pair": xm.group(2)}

    return analyses, live


def _cycle_timestamp(report_path: Path) -> int:
    """Parse UTC Unix timestamp from a brain report filename."""
    stem = report_path.stem  # "brain_2026-04-12_0221"
    _, date, timestr = stem.split("_", 2)
    dt = datetime.strptime(f"{date}_{timestr}", "%Y-%m-%d_%H%M").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _pair_to_kraken_key(pair: str, pair_index: dict[str, str]) -> str | None:
    """Map a normalized pair like 'BTC/USD' to the Kraken API key (e.g. 'XXBTZUSD')."""
    key = pair_index.get(pair)
    if key:
        return key
    # Fallback: try alias-reverse (BTC -> XBT) and simple concatenation
    alt = pair.replace("BTC/", "XBT/").replace("/BTC", "/XBT")
    return pair_index.get(alt)


def _fetch_forward_return(kraken_key: str, cycle_ts: int, window_h: int) -> float | None:
    """Fetch hourly OHLC starting at cycle_ts and return (close[window_h] / close[0]) - 1.

    Returns None if Kraken has insufficient bars for the requested window.
    """
    try:
        url = f"{_KRAKEN_OHLC}?pair={kraken_key}&interval=60&since={cycle_ts - 3600}"
        with urllib.request.urlopen(url, timeout=15) as r:
            body = json.load(r)
    except Exception:
        return None
    result = body.get("result", {})
    # Kraken returns {<key>: [bars], "last": ts}. Key differs from our request.
    bars = None
    for k, v in result.items():
        if isinstance(v, list) and v:
            bars = v
            break
    if not bars or len(bars) < window_h + 1:
        return None
    try:
        entry_close = float(bars[0][4])
        exit_close = float(bars[window_h][4])
    except (IndexError, ValueError):
        return None
    if entry_close <= 0:
        return None
    return (exit_close / entry_close) - 1.0


def _score_self_check(analyses: list[dict], expected_scores: dict[str, float]) -> tuple[int, int]:
    """Sanity: confirm reconstructed score_entry matches the report's logged score."""
    hits, total = 0, 0
    for a in analyses:
        exp = expected_scores.get(a["pair"])
        if exp is None:
            continue
        total += 1
        got, _ = score_entry(a)
        if abs(got - exp) < 0.05:  # tolerance: 0.05 covers reconstruction rounding
            hits += 1
    return hits, total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Only process the N most recent reports")
    ap.add_argument("--verbose", action="store_true",
                    help="Print per-cycle details")
    ap.add_argument("--forward-hours", type=int, default=2,
                    help="Forward-return evaluation window in hours (default 2)")
    ap.add_argument("--no-forward", action="store_true",
                    help="Skip forward-return analysis (faster)")
    args = ap.parse_args()

    reports = sorted(_REPORTS_DIR.glob("brain_*.md"))
    if args.limit:
        reports = reports[-args.limit:]
    if not reports:
        print(f"No brain reports found in {_REPORTS_DIR}")
        return

    print(f"Backfilling {len(reports)} historical brain cycles\n")

    cycles: list[dict] = []
    recon_hits, recon_total = 0, 0

    for path in reports:
        analyses, live = _parse_report(path)
        if not analyses:
            continue

        # Verify reconstruction fidelity: does score_entry on the rebuilt
        # analysis produce the same score the original report logged?
        text = path.read_text(encoding="utf-8", errors="replace")
        expected = {}
        for line in text.splitlines():
            am = _ANALYSIS_LINE.match(line)
            if am:
                expected[am.group(1)] = float(am.group(8))
        hits, total = _score_self_check(analyses, expected)
        recon_hits += hits
        recon_total += total

        unified = compute_unified_holds(analyses)
        eligible = sorted(
            [(a, v) for a, v in unified.items() if v["eligible"]],
            key=lambda x: -x[1]["top3_mean"],
        )
        shadow_best = eligible[0][0] if eligible else None

        cycle = {
            "file": path.name,
            "n_analyses": len(analyses),
            "live": live,
            "shadow_best": shadow_best,
            "eligible_assets": [a for a, _ in eligible],
            "top_eligible": eligible[:5],
        }
        cycles.append(cycle)

        if args.verbose:
            print(f"{path.name}: live={live.get('type')}:{live.get('asset', '-')} "
                  f"shadow={shadow_best}  n_eligible={len(eligible)}")

    if not cycles:
        print("No cycles with parseable analyses.")
        return

    # === Reconstruction sanity ===
    print("=== Reconstruction sanity ===")
    if recon_total:
        acc = 100 * recon_hits / recon_total
        print(f"  score_entry recovery: {recon_hits}/{recon_total} ({acc:.0f}%) within 0.05 of logged score")
    print()

    # === Overall stats ===
    print("=== Backfill summary ===")
    print(f"  Cycles analyzed: {len(cycles)}")
    print(f"  Avg analyses/cycle: {sum(c['n_analyses'] for c in cycles) / len(cycles):.1f}")
    eligible_counts = [len(c["eligible_assets"]) for c in cycles]
    print(f"  Avg eligible assets/cycle: {sum(eligible_counts) / len(cycles):.1f}")
    print()

    # === Live decision types ===
    live_types = Counter(c["live"].get("type") for c in cycles)
    print("=== Live decision types ===")
    for t, n in live_types.most_common():
        print(f"  {t:10s} {n:3d}  ({100*n/len(cycles):.0f}%)")
    print()

    # === Shadow picks ===
    shadow_picks = Counter(c["shadow_best"] for c in cycles)
    print("=== Shadow 'best hold' picks ===")
    for asset, n in shadow_picks.most_common(10):
        print(f"  {asset or '(none)':10s} {n:3d}  ({100*n/len(cycles):.0f}%)")
    print()

    # === Live targets ===
    live_targets = Counter(c["live"].get("asset", "-") for c in cycles)
    print("=== Live decision targets ===")
    for asset, n in live_targets.most_common(10):
        print(f"  {asset:10s} {n:3d}  ({100*n/len(cycles):.0f}%)")
    print()

    # === Agreement on entry/rotation cycles ===
    comparable = [c for c in cycles if c["live"].get("type") in ("entry", "rotation")]
    agreements = sum(1 for c in comparable if c["shadow_best"] == c["live"].get("asset"))
    if comparable:
        rate = 100 * agreements / len(comparable)
        print("=== Agreement on order cycles ===")
        print(f"  {agreements}/{len(comparable)} ({rate:.0f}%) — shadow and live picked same asset")
        if agreements < len(comparable):
            dis = Counter(
                (c["shadow_best"] or "(none)", c["live"].get("asset"))
                for c in comparable if c["shadow_best"] != c["live"].get("asset")
            )
            print("\n  Top disagreement patterns (shadow -> live):")
            for (s, l), n in dis.most_common(10):
                print(f"    {s:8s} -> {l:8s}  {n:3d}x")
        print()

    # === Eligibility coverage per asset ===
    asset_seen = Counter()
    for c in cycles:
        for a in c["eligible_assets"]:
            asset_seen[a] += 1
    print("=== Eligibility coverage (how often each asset had n >= 3) ===")
    for asset, n in asset_seen.most_common(15):
        pct = 100 * n / len(cycles)
        print(f"  {asset:10s} {n:3d}/{len(cycles):3d}  ({pct:.0f}%)")

    # === Forward-return comparison ===
    if args.no_forward:
        return
    print(f"\n=== Forward-return comparison ({args.forward_hours}h window) ===")
    # Build pair index: "BTC/USD" -> Kraken key like "XXBTZUSD"
    try:
        kraken_pairs = _fetch_kraken_pairs()
    except Exception as e:
        print(f"  Unable to fetch Kraken pairs: {e}")
        return
    pair_index = {norm: info["key"] for norm, info in kraken_pairs.items()}

    now_ts = int(time.time())
    min_age_s = (args.forward_hours + 1) * 3600  # +1h buffer for bar alignment

    evaluable: list[dict] = []
    for c in cycles:
        ts = _cycle_timestamp(Path(_REPORTS_DIR) / c["file"])
        if now_ts - ts < min_age_s:
            continue  # too recent — forward window not yet available

        live = c["live"]
        shadow_asset = c["shadow_best"]
        if live.get("type") not in ("entry", "rotation") or not shadow_asset:
            continue

        # Live pair (what it bought into)
        live_pair = live.get("pair")
        if not live_pair:
            continue
        live_key = _pair_to_kraken_key(live_pair, pair_index)
        if not live_key:
            continue
        live_ret = _fetch_forward_return(live_key, ts, args.forward_hours)
        if live_ret is None:
            continue

        # Shadow pick's pair (against USD)
        if shadow_asset == "USD":
            shadow_ret = 0.0  # holding cash = zero return
            shadow_pair = "USD"
        else:
            shadow_pair = f"{shadow_asset}/USD"
            shadow_key = _pair_to_kraken_key(shadow_pair, pair_index)
            if not shadow_key:
                continue
            shadow_ret = _fetch_forward_return(shadow_key, ts, args.forward_hours)
            if shadow_ret is None:
                continue

        evaluable.append({
            "file": c["file"],
            "ts": ts,
            "live_pair": live_pair,
            "live_ret": live_ret,
            "shadow_pair": shadow_pair,
            "shadow_ret": shadow_ret,
            "diff": live_ret - shadow_ret,  # positive = live better
        })

    if not evaluable:
        print("  No cycles old enough for the forward window. "
              "Let more time pass and re-run.")
        return

    print(f"  {len(evaluable)}/{len(cycles)} cycles had sufficient forward data\n")
    print(f"  {'cycle':20s} {'live':14s} {'live%':>8s} {'shadow':10s} {'shad%':>8s} {'diff':>8s}  verdict")
    print("  " + "-" * 85)
    for e in sorted(evaluable, key=lambda x: x["ts"]):
        verdict = "LIVE" if e["diff"] > 0.001 else ("SHADOW" if e["diff"] < -0.001 else "tie")
        dt = datetime.fromtimestamp(e["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
        print(f"  {dt:20s} {e['live_pair']:14s} {e['live_ret']*100:+7.2f}% "
              f"{e['shadow_pair']:10s} {e['shadow_ret']*100:+7.2f}% "
              f"{e['diff']*100:+7.2f}%  {verdict}")

    live_wins = sum(1 for e in evaluable if e["diff"] > 0.001)
    shadow_wins = sum(1 for e in evaluable if e["diff"] < -0.001)
    ties = len(evaluable) - live_wins - shadow_wins
    live_total = sum(e["live_ret"] for e in evaluable)
    shadow_total = sum(e["shadow_ret"] for e in evaluable)
    avg_diff = sum(e["diff"] for e in evaluable) / len(evaluable)

    print("\n  === Tally ===")
    print(f"  Live won:    {live_wins} ({100*live_wins/len(evaluable):.0f}%)")
    print(f"  Shadow won:  {shadow_wins} ({100*shadow_wins/len(evaluable):.0f}%)")
    print(f"  Ties:        {ties}")
    print(f"  Cumulative: live {live_total*100:+.2f}%, shadow {shadow_total*100:+.2f}%")
    print(f"  Avg per-cycle edge: {avg_diff*100:+.3f}% (positive = live better)")


if __name__ == "__main__":
    main()
