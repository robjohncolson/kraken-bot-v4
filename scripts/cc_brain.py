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
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BOT_URL = "http://127.0.0.1:58392"
KRAKEN_API = "https://api.kraken.com/0/public"
REVIEWS_DIR = Path(__file__).parent.parent / "state" / "cc-reviews"

def _price_decimals(price: float) -> int:
    """Determine appropriate decimal places for a limit price on Kraken."""
    if price >= 10:
        return 2      # SOL ($85), LTC ($55), XMR ($338)
    if price >= 1:
        return 4      # XRP ($1.35), ADA ($0.25)
    if price >= 0.01:
        return 5      # DOGE ($0.09)
    return 6          # PEPE ($0.0000036)


# Strategy parameters
MAX_POSITION_PCT = 0.04      # 4% of portfolio per position
DUST_THRESHOLD_PCT = 0.01    # 1% of portfolio — below this is dust
MIN_REGIME_GATE = 0.15       # Absolute floor — below this, don't even score
SOFT_REGIME_GATE = 0.40      # Below this, score is capped (visible but won't trigger entry)
SOFT_REGIME_CAP = 0.5        # Max score when trade_gate is in [MIN, SOFT) range
ENTRY_THRESHOLD = 0.6        # Must exceed this to place an order
MIN_RSI_OVERSOLD = 35        # RSI below this = oversold (potential buy)
MAX_RSI_OVERBOUGHT = 70      # RSI above this = overbought (potential sell)
TARGET_MONTHLY_PCT = 1.0     # 1% monthly target
TOP_PAIRS = [
    "SOL/USD", "BTC/USD", "ETH/USD", "AVAX/USD", "LINK/USD",
    "AAVE/USD", "DOT/USD", "ATOM/USD", "ADA/USD", "MATIC/USD",
    "CRV/USD", "UNI/USD", "DOGE/USD", "NEAR/USD", "FTM/USD",
]


# Symbol aliases: Kraken wsname → standard
_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}
# Assets to exclude from trading (stablecoins, fiat)
_SKIP_BASES = frozenset(("USDT", "USDC", "DAI", "PYUSD", "EUR", "GBP", "AUD", "CAD", "CHF", "JPY"))

# Pair discovery cache
_discovered_cache: dict = {}
_discovered_at: float = 0.0
_DISCOVERY_TTL = 3600  # 1 hour cache


def compute_stability(volume_usd_24h: float, volatility_pct: float) -> float:
    """Asset stability: 0.0 = volatile micro-cap, 1.0 = stable blue-chip.

    Uses 24h USD volume as market-cap proxy and predicted volatility as risk.
    """
    import math
    vol_score = min(1.0, max(0.0, (math.log10(max(1, volume_usd_24h)) - 4.7) / 4.3))
    vol_penalty = min(1.0, volatility_pct / 10.0)  # 10%+ daily vol = full penalty
    return round(vol_score * (1.0 - 0.5 * vol_penalty), 4)


def _fetch_kraken_pairs() -> dict[str, dict]:
    """Fetch AssetPairs from Kraken. Returns {normalized -> {key, base, quote}}. Cached 1h."""
    global _discovered_cache, _discovered_at
    if "pairs" in _discovered_cache and (time.time() - _discovered_at) < _DISCOVERY_TTL:
        return _discovered_cache["pairs"]

    req = urllib.request.Request(f"{KRAKEN_API}/AssetPairs")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())
    pairs_data = body.get("result", {})

    all_pairs: dict[str, dict] = {}
    for key, meta in pairs_data.items():
        if ".d" in key:
            continue
        wsname = meta.get("wsname", "")
        if not wsname or "/" not in wsname:
            continue
        base, quote = wsname.split("/", 1)
        base = _ALIASES.get(base, base)
        quote = _ALIASES.get(quote, quote)
        if base in _SKIP_BASES and quote in _SKIP_BASES:
            continue
        all_pairs[f"{base}/{quote}"] = {"key": key, "base": base, "quote": quote}

    _discovered_cache["pairs"] = all_pairs
    _discovered_at = time.time()
    return all_pairs


def _fetch_tickers(pairs: dict[str, dict]) -> dict[str, dict]:
    """Fetch Ticker data for a set of pairs. Returns {normalized -> ticker_data}."""
    if not pairs:
        return {}
    pair_keys = ",".join(p["key"] for p in pairs.values())
    req = urllib.request.Request(f"{KRAKEN_API}/Ticker?pair={pair_keys}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        ticker_body = json.loads(resp.read().decode())
    raw_tickers = ticker_body.get("result", {})
    key_to_norm = {v["key"]: k for k, v in pairs.items()}
    return {key_to_norm[k]: v for k, v in raw_tickers.items() if k in key_to_norm}


def discover_all_pairs(
    min_volume_usd: float = 50_000, limit: int = 40,
) -> list[dict]:
    """Discover all liquid Kraken spot pairs ranked by 24h USD volume.

    Returns [{pair, base, quote, volume_usd}, ...].
    """
    try:
        all_pairs = _fetch_kraken_pairs()
        # Only fetch tickers for USD-quoted pairs (liquid, priceable)
        usd_pairs = {n: p for n, p in all_pairs.items()
                     if p["quote"] == "USD" and p["base"] not in _SKIP_BASES}
        tickers = _fetch_tickers(usd_pairs)
    except Exception:
        return [{"pair": p, "base": p.split("/")[0], "quote": "USD", "volume_usd": 0}
                for p in TOP_PAIRS]

    ranked: list[dict] = []
    for norm, ticker in tickers.items():
        info = usd_pairs.get(norm)
        if not info:
            continue
        vol_24h = float(ticker["v"][1])
        last_price = float(ticker["c"][0])
        volume_usd = vol_24h * last_price
        if volume_usd >= min_volume_usd:
            ranked.append({
                "pair": norm, "base": info["base"], "quote": info["quote"],
                "volume_usd": volume_usd,
            })

    ranked.sort(key=lambda x: -x["volume_usd"])
    return ranked[:limit]


def get_asset_volumes() -> dict[str, float]:
    """Get 24h USD volume per asset (sum across all USD pairs)."""
    try:
        all_pairs = _fetch_kraken_pairs()
        usd_pairs = {n: p for n, p in all_pairs.items() if p["quote"] == "USD"}
        tickers = _fetch_tickers(usd_pairs)
    except Exception:
        return {}

    volumes: dict[str, float] = {}
    for norm, ticker in tickers.items():
        info = usd_pairs.get(norm)
        if not info:
            continue
        vol_usd = float(ticker["v"][1]) * float(ticker["c"][0])
        volumes[info["base"]] = volumes.get(info["base"], 0) + vol_usd
    return volumes


def compute_portfolio_value() -> tuple[float, list[dict]]:
    """Compute true portfolio value from exchange balances + ticker prices.

    Returns (total_usd, [{asset, qty, price_usd, value_usd}, ...]).
    """
    # Get live balances from Kraken via bot
    bal_resp = fetch("/api/exchange-balances")
    if "error" in bal_resp or "balances" not in bal_resp:
        return 0.0, []

    # Build USD price map — fetch only pairs involving held assets
    usd_prices: dict[str, float] = {"USD": 1.0, "USDT": 1.0, "USDC": 1.0}
    held_assets = {_ALIASES.get(b["asset"], b["asset"]) for b in bal_resp["balances"]
                   if (float(b["available"]) + float(b["held"])) > 0}
    try:
        all_pairs = _fetch_kraken_pairs()
        # Find pairs that can price held assets: {asset}/USD, USD/{asset}, BTC/{asset}
        pricing_pairs = {n: p for n, p in all_pairs.items()
                         if (p["base"] in held_assets or p["quote"] in held_assets)
                         and (p["quote"] == "USD" or p["base"] == "USD" or p["base"] == "BTC")}
        tickers = _fetch_tickers(pricing_pairs)

        for norm, tdata in tickers.items():
            info = pricing_pairs.get(norm)
            if not info:
                continue
            price = float(tdata["c"][0])
            if info["quote"] == "USD":
                usd_prices[info["base"]] = price
            elif info["base"] == "USD" and price > 0:
                usd_prices[info["quote"]] = 1.0 / price

        # Cross-rates via BTC for remaining unpriced assets
        btc_usd = usd_prices.get("BTC", 0)
        if btc_usd > 0:
            for norm, tdata in tickers.items():
                info = pricing_pairs.get(norm)
                if not info or info["base"] != "BTC":
                    continue
                if info["quote"] not in usd_prices:
                    btc_in_quote = float(tdata["c"][0])
                    if btc_in_quote > 0:
                        usd_prices[info["quote"]] = btc_usd / btc_in_quote
    except Exception:
        pass

    # Price each held asset
    holdings: list[dict] = []
    total = 0.0
    for b in bal_resp["balances"]:
        asset = b["asset"]
        qty = float(b["available"]) + float(b["held"])
        if qty <= 0:
            continue
        # Normalize raw Kraken symbol for price lookup
        norm_asset = _ALIASES.get(asset, asset)
        price = usd_prices.get(norm_asset, 0)
        # If no price from ticker or cross-rate, try bot's OHLCV
        if price == 0:
            ohlcv = fetch(f"/api/ohlcv/{norm_asset}%2FUSD?interval=60&count=1")
            bars = ohlcv.get("bars", [])
            if bars:
                price = float(bars[-1]["close"])
                usd_prices[norm_asset] = price
        value = qty * price
        total += value
        holdings.append({
            "asset": norm_asset, "raw_asset": asset,
            "qty": qty, "price_usd": price, "value_usd": round(value, 2),
        })

    holdings.sort(key=lambda h: -h["value_usd"])
    return round(total, 2), holdings


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
    except urllib.error.HTTPError as exc:
        # Read the response body for detailed error info
        try:
            body = json.loads(exc.read().decode())
            detail = body.get("detail", str(exc))
        except Exception:
            detail = str(exc)
        return {"error": detail}
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
    """Full analysis: regime + RSI + EMA + Kronos + TimesFM."""
    enc = pair.replace("/", "%2F")

    # Regime (HMM)
    regime_data = fetch(f"/api/regime/{enc}?interval=60&count=300")
    if "error" in regime_data:
        return None

    # 1H bars for RSI + EMA
    ohlcv_1h = fetch(f"/api/ohlcv/{enc}?interval=60&count=50")
    if "error" in ohlcv_1h or not ohlcv_1h.get("bars"):
        return None
    closes_1h = [float(b["close"]) for b in ohlcv_1h["bars"]]

    # 4H bars for trend
    ohlcv_4h = fetch(f"/api/ohlcv/{enc}?interval=240&count=50")
    closes_4h = (
        [float(b["close"]) for b in ohlcv_4h.get("bars", [])]
        if "error" not in ohlcv_4h else []
    )

    # Kronos prediction (full OHLCV candle, ~4s)
    kronos = fetch(f"/api/kronos/{enc}?interval=60&pred_len=24")

    # TimesFM prediction (close-price trajectory, ~6s)
    timesfm = fetch(f"/api/timesfm/{enc}")

    # Compute signals
    rsi_1h = compute_rsi(closes_1h)
    ema7_1h = compute_ema(closes_1h, 7) if len(closes_1h) >= 7 else closes_1h[-1]
    ema26_1h = compute_ema(closes_1h, 26) if len(closes_1h) >= 26 else closes_1h[-1]
    trend_1h = "UP" if ema7_1h > ema26_1h else "DOWN"

    ema7_4h = compute_ema(closes_4h, 7) if len(closes_4h) >= 7 else None
    ema26_4h = compute_ema(closes_4h, 26) if len(closes_4h) >= 26 else None
    trend_4h = (
        "UP" if (ema7_4h and ema26_4h and ema7_4h > ema26_4h)
        else "DOWN" if ema7_4h else "UNKNOWN"
    )

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
        "timesfm_direction": timesfm.get("direction", "unknown"),
        "timesfm_confidence": timesfm.get("confidence", 0),
    }


def score_entry(analysis: dict) -> tuple[float, dict]:
    """Score a pair for entry. Returns (score, breakdown) where breakdown shows each component."""
    breakdown: dict[str, float] = {}

    # Hard floor: truly untradeable regime
    if analysis["trade_gate"] < MIN_REGIME_GATE:
        return 0.0, {"gate": "regime below floor"}

    # Soft regime gate: score is capped if trade_gate < SOFT_REGIME_GATE
    soft_capped = analysis["trade_gate"] < SOFT_REGIME_GATE

    # 4H trend component (was a hard gate — now weighted)
    trend_4h = analysis["trend_4h"]
    if trend_4h == "UP":
        breakdown["4H_trend"] = 0.20
    elif trend_4h == "DOWN":
        breakdown["4H_trend"] = -0.15
    else:
        breakdown["4H_trend"] = 0.0

    # 1H trend component (previously unused)
    if analysis["trend_1h"] == "UP":
        breakdown["1H_trend"] = 0.10
    else:
        breakdown["1H_trend"] = 0.0

    # RSI component: oversold in uptrend = dip-buy opportunity
    rsi = analysis["rsi_1h"]
    if rsi < MIN_RSI_OVERSOLD:
        breakdown["RSI"] = 0.40
    elif rsi < 50:
        breakdown["RSI"] = 0.20
    else:
        breakdown["RSI"] = 0.0

    # Kronos component (full OHLCV candle prediction)
    kdir = analysis["kronos_direction"]
    if kdir == "bullish":
        breakdown["Kronos"] = 0.20
    elif kdir == "neutral":
        breakdown["Kronos"] = 0.05
    else:
        breakdown["Kronos"] = 0.0

    # TimesFM component (close-price trajectory forecast)
    tfm_dir = analysis.get("timesfm_direction", "unknown")
    tfm_conf = float(analysis.get("timesfm_confidence", 0))
    if tfm_dir == "bullish":
        breakdown["TimesFM"] = round(0.20 * tfm_conf, 3)
    elif tfm_dir == "neutral":
        breakdown["TimesFM"] = 0.0
    else:
        breakdown["TimesFM"] = round(-0.10 * tfm_conf, 3)

    # Regime component: trending is ideal
    regime = analysis["regime"]
    if regime == "trending":
        breakdown["regime"] = 0.30
    elif regime == "volatile":
        breakdown["regime"] = 0.10
    else:
        breakdown["regime"] = 0.0

    raw = sum(breakdown.values())
    score = min(1.0, max(0.0, raw))

    # Apply soft cap if regime is weak but not dead
    if soft_capped:
        score = min(score, SOFT_REGIME_CAP)
        breakdown["cap"] = SOFT_REGIME_CAP

    return score, breakdown


def score_hold(analysis: dict, stability: float) -> float:
    """Score for continuing to hold an asset. Stability provides a holding bonus."""
    entry_score, _ = score_entry(analysis)
    hold_bonus = stability * 0.3  # BTC gets +0.29, micro-cap gets +0.03
    return min(1.0, entry_score + hold_bonus)


def rotation_threshold(source_stability: float) -> float:
    """Minimum score improvement needed to rotate away from source asset.

    Stable assets (BTC, ETH) need a bigger improvement to justify rotation.
    """
    return 0.10 + 0.20 * source_stability  # 0.10 for volatile micro, 0.30 for BTC


def evaluate_portfolio(
    positions: list[dict],
    analyses: list[dict],
    stabilities: dict[str, float],
    all_pairs: list[dict],
) -> list[dict]:
    """For each position, find the best rotation target.

    Returns sorted list of proposals: [{from_asset, to_asset, pair, side,
    improvement, target_score, source_hold_score}, ...]
    """
    # Index analyses by base asset for quick lookup
    analysis_by_base: dict[str, dict] = {}
    for a in analyses:
        base = a["pair"].split("/")[0]
        # Keep the highest-scoring analysis per base asset
        if base not in analysis_by_base or a["_score"] > analysis_by_base[base]["_score"]:
            analysis_by_base[base] = a

    # Index available pairs for routing
    pair_lookup: dict[tuple[str, str], dict] = {}
    for p in all_pairs:
        pair_lookup[(p["base"], p["quote"])] = p

    proposals: list[dict] = []
    for pos in positions:
        from_asset = pos["asset"]
        if from_asset in QUOTE_CURRENCIES:
            continue  # USD/USDT/USDC evaluated separately as "cash to deploy"
        from_stab = stabilities.get(from_asset, 0)
        from_analysis = analysis_by_base.get(from_asset)
        if not from_analysis:
            continue
        hold_score = score_hold(from_analysis, from_stab)
        threshold = rotation_threshold(from_stab)

        for to_asset, to_analysis in analysis_by_base.items():
            if to_asset == from_asset:
                continue
            to_score = to_analysis["_score"]
            improvement = to_score - hold_score
            if improvement < threshold:
                continue

            # Find a direct pair for this rotation
            pair_info = pair_lookup.get((from_asset, to_asset)) or pair_lookup.get((to_asset, from_asset))
            if not pair_info:
                continue  # no direct pair — skip (multi-hop is future work)

            pair = pair_info["pair"]
            side = "sell" if pair_info["base"] == from_asset else "buy"
            proposals.append({
                "from_asset": from_asset,
                "to_asset": to_asset,
                "pair": pair,
                "side": side,
                "improvement": round(improvement, 3),
                "target_score": round(to_score, 3),
                "source_hold_score": round(hold_score, 3),
                "threshold": round(threshold, 3),
            })

    proposals.sort(key=lambda p: -p["improvement"])
    return proposals


QUOTE_CURRENCIES = frozenset(("USD", "USDT", "USDC"))  # can't sell these as dust
STALE_ORDER_HOURS = 2  # cancel unfilled orders after this

# Self-tuning bounds
_ENTRY_THRESHOLD_MIN = 0.50
_ENTRY_THRESHOLD_MAX = 0.85
_POSITION_PCT_MIN = 0.02
_POSITION_PCT_MAX = 0.08
_REGIME_GATE_MIN = 0.10
_REGIME_GATE_MAX = 0.30


def self_tune(outcomes: list[dict], analyses: list[dict], log_fn) -> None:
    """Adjust strategy parameters based on post-mortem patterns. Max 1 change per cycle."""
    global ENTRY_THRESHOLD, MAX_POSITION_PCT, MIN_REGIME_GATE

    if not outcomes or len(outcomes) < 5:
        # Not enough data to tune — but check if brain is idle
        if analyses and all(a.get("_score", 0) < ENTRY_THRESHOLD for a in analyses):
            if MIN_REGIME_GATE > _REGIME_GATE_MIN:
                old = MIN_REGIME_GATE
                MIN_REGIME_GATE = round(MIN_REGIME_GATE - 0.05, 2)
                log_fn(f"  TUNE: MIN_REGIME_GATE {old} -> {MIN_REGIME_GATE} (brain idle, widening filter)")
                _record_param_change("MIN_REGIME_GATE", old, MIN_REGIME_GATE, "brain idle — no pairs above threshold")
        return

    wins = sum(1 for t in outcomes if float(t.get("net_pnl", 0)) > 0)
    total_pnl = sum(float(t.get("net_pnl", 0)) for t in outcomes)
    gross_wins = sum(float(t["net_pnl"]) for t in outcomes if float(t.get("net_pnl", 0)) > 0)
    total_fees = sum(abs(float(t.get("fee_total", 0))) for t in outcomes)
    sl_exits = sum(1 for t in outcomes if t.get("exit_reason") == "stop_loss")
    wr = wins / len(outcomes)

    # Rule 1: Win rate too low — tighten entry
    if wr < 0.30 and ENTRY_THRESHOLD < _ENTRY_THRESHOLD_MAX:
        old = ENTRY_THRESHOLD
        ENTRY_THRESHOLD = round(ENTRY_THRESHOLD + 0.05, 2)
        log_fn(f"  TUNE: ENTRY_THRESHOLD {old} -> {ENTRY_THRESHOLD} (WR={wr:.0%} < 30%)")
        _record_param_change("ENTRY_THRESHOLD", old, ENTRY_THRESHOLD, f"win rate {wr:.0%} below 30%")
        return

    # Rule 2: Win rate high — relax entry
    if wr > 0.60 and ENTRY_THRESHOLD > _ENTRY_THRESHOLD_MIN:
        old = ENTRY_THRESHOLD
        ENTRY_THRESHOLD = round(ENTRY_THRESHOLD - 0.05, 2)
        log_fn(f"  TUNE: ENTRY_THRESHOLD {old} -> {ENTRY_THRESHOLD} (WR={wr:.0%} > 60%)")
        _record_param_change("ENTRY_THRESHOLD", old, ENTRY_THRESHOLD, f"win rate {wr:.0%} above 60%")
        return

    # Rule 3: Fee burden too high — increase position size
    if gross_wins > 0 and total_fees / gross_wins > 0.60 and MAX_POSITION_PCT < _POSITION_PCT_MAX:
        old = MAX_POSITION_PCT
        MAX_POSITION_PCT = round(MAX_POSITION_PCT + 0.01, 2)
        fee_pct = total_fees / gross_wins
        log_fn(f"  TUNE: MAX_POSITION_PCT {old} -> {MAX_POSITION_PCT} (fees={fee_pct:.0%} of wins)")
        _record_param_change("MAX_POSITION_PCT", old, MAX_POSITION_PCT, f"fee burden {fee_pct:.0%}")
        return

    # Rule 4: Too many stop-loss exits — tighten regime gate
    sl_rate = sl_exits / len(outcomes)
    if sl_rate > 0.60 and MIN_REGIME_GATE < _REGIME_GATE_MAX:
        old = MIN_REGIME_GATE
        MIN_REGIME_GATE = round(MIN_REGIME_GATE + 0.05, 2)
        log_fn(f"  TUNE: MIN_REGIME_GATE {old} -> {MIN_REGIME_GATE} (SL exits={sl_rate:.0%})")
        _record_param_change("MIN_REGIME_GATE", old, MIN_REGIME_GATE, f"stop-loss exit rate {sl_rate:.0%}")
        return

    log_fn(f"  No tuning needed (WR={wr:.0%}, P&L=${total_pnl:.2f}, {len(outcomes)} trades)")


def _record_param_change(param: str, old, new, reason: str) -> None:
    fetch("/api/memory", method="POST", data={
        "category": "param_change",
        "content": {"param": param, "old": str(old), "new": str(new), "reason": reason},
        "importance": 0.9,
    })


DEEP_POSTMORTEM_INTERVAL_HOURS = 72  # full review every 3 days


def immediate_postmortem(outcomes: list[dict], log_fn) -> list[dict]:
    """Analyze trades that didn't go as predicted. Returns list of findings."""
    # Check which outcomes are new since last cycle (closed in last 2h)
    findings: list[dict] = []
    for t in outcomes:
        pnl = float(t.get("net_pnl", 0))
        if pnl >= 0:
            continue  # trade went fine — skip
        pair = t.get("pair", "?")
        exit_reason = t.get("exit_reason", "?")
        hold_hours = float(t.get("hold_hours") or 0)
        entry_price = float(t.get("entry_price") or 0)
        exit_price = float(t.get("exit_price") or 0)
        confidence = float(t.get("confidence") or 0)

        # Diagnose: what went wrong?
        diagnosis: list[str] = []
        if exit_reason == "stop_loss" and hold_hours < 1:
            diagnosis.append("quick_sl_hit")
        if exit_reason == "stop_loss" and confidence >= 0.8:
            diagnosis.append("high_confidence_loss")
        if exit_reason == "timer":
            diagnosis.append("timed_out_no_movement")
        if entry_price > 0 and exit_price > 0:
            loss_pct = abs(exit_price - entry_price) / entry_price * 100
            if loss_pct > 3:
                diagnosis.append(f"large_loss_{loss_pct:.1f}pct")

        # Get current regime for context
        regime_data = fetch(f"/api/regime/{pair.replace('/', '%2F')}?interval=60&count=300")
        current_regime = regime_data.get("regime", "?") if "error" not in regime_data else "?"

        finding = {
            "pair": pair, "pnl": round(pnl, 4), "exit_reason": exit_reason,
            "hold_hours": round(hold_hours, 2), "confidence": confidence,
            "diagnosis": diagnosis, "current_regime": current_regime,
        }
        findings.append(finding)

        # Write to memory
        fetch("/api/memory", method="POST", data={
            "category": "postmortem", "pair": pair,
            "content": finding, "importance": 0.7,
        })
        log_fn(f"  PM: {pair} lost ${abs(pnl):.4f} ({exit_reason}, {hold_hours:.1f}h) "
               f"— {', '.join(diagnosis) or 'no_pattern'}")

    return findings


def deep_postmortem(log_fn) -> None:
    """Periodic deep review — aggregate patterns from recent postmortems. Runs every 72h."""
    # Check when last deep PM ran
    last_deep = fetch("/api/memory?category=deep_postmortem&hours=168&limit=1")
    if "error" not in last_deep:
        mems = last_deep.get("memories", [])
        if mems:
            # Parse timestamp to check age
            last_ts = mems[0].get("timestamp", "")
            if last_ts:
                try:
                    last_dt = datetime.fromisoformat(last_ts)
                    age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                    if age_hours < DEEP_POSTMORTEM_INTERVAL_HOURS:
                        return  # too recent, skip
                except (ValueError, TypeError):
                    pass

    # Gather all postmortem memories from last 72h
    pms = fetch(f"/api/memory?category=postmortem&hours={DEEP_POSTMORTEM_INTERVAL_HOURS}")
    if "error" in pms:
        return
    pm_list = pms.get("memories", [])
    if len(pm_list) < 3:
        return  # not enough data for deep analysis

    log_fn(f"\n  === Deep Post-Mortem ({len(pm_list)} trades reviewed) ===")

    # Aggregate patterns
    pair_losses: dict[str, list[float]] = {}
    diagnosis_counts: dict[str, int] = {}
    exit_reason_counts: dict[str, int] = {}
    for pm in pm_list:
        c = pm.get("content", {})
        pair = c.get("pair", "?")
        pnl = c.get("pnl", 0)
        pair_losses.setdefault(pair, []).append(pnl)
        for d in c.get("diagnosis", []):
            diagnosis_counts[d] = diagnosis_counts.get(d, 0) + 1
        er = c.get("exit_reason", "?")
        exit_reason_counts[er] = exit_reason_counts.get(er, 0) + 1

    # Report: repeat losers
    repeat_losers = [(p, losses) for p, losses in pair_losses.items() if len(losses) >= 2]
    if repeat_losers:
        log_fn("  Repeat losers:")
        for pair, losses in sorted(repeat_losers, key=lambda x: sum(x[1])):
            log_fn(f"    {pair}: {len(losses)} losses, total ${sum(losses):.4f}")

    # Report: common diagnoses
    if diagnosis_counts:
        log_fn("  Common patterns:")
        for diag, count in sorted(diagnosis_counts.items(), key=lambda x: -x[1]):
            log_fn(f"    {diag}: {count} occurrences")

    # Report: exit reasons
    log_fn("  Exit reasons:")
    for er, count in sorted(exit_reason_counts.items(), key=lambda x: -x[1]):
        log_fn(f"    {er}: {count}")

    # Build summary for report file
    summary = {
        "period_hours": DEEP_POSTMORTEM_INTERVAL_HOURS,
        "trades_reviewed": len(pm_list),
        "repeat_losers": {p: len(losses) for p, losses in repeat_losers},
        "diagnosis_counts": diagnosis_counts,
        "exit_reason_counts": exit_reason_counts,
        "total_loss": round(sum(pnl for losses in pair_losses.values() for pnl in losses), 4),
    }

    # Save report file
    now = datetime.now(timezone.utc)
    report_path = REVIEWS_DIR / f"deep_pm_{now.strftime('%Y-%m-%d_%H%M')}.md"
    report_lines = [
        f"# Deep Post-Mortem — {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"\nTrades reviewed: {len(pm_list)}",
        f"Total loss: ${summary['total_loss']:.4f}",
    ]
    if repeat_losers:
        report_lines.append("\n## Repeat Losers")
        for pair, losses in sorted(repeat_losers, key=lambda x: sum(x[1])):
            report_lines.append(f"- {pair}: {len(losses)} losses, ${sum(losses):.4f}")
    if diagnosis_counts:
        report_lines.append("\n## Patterns")
        for diag, count in sorted(diagnosis_counts.items(), key=lambda x: -x[1]):
            report_lines.append(f"- {diag}: {count}x")
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    log_fn(f"  Deep PM report: {report_path}")

    # Write to memory
    fetch("/api/memory", method="POST", data={
        "category": "deep_postmortem",
        "content": summary, "importance": 0.9,
    })


def check_pending_orders(log_fn, dry_run: bool) -> None:
    """Cancel brain-placed orders that haven't filled within STALE_ORDER_HOURS."""
    pending = fetch("/api/memory?category=pending_order&hours=24")
    if "error" in pending:
        return
    now_ts = time.time()
    for mem in pending.get("memories", []):
        content = mem.get("content", {})
        txid = content.get("txid")
        placed_ts = content.get("placed_ts", 0)
        if not txid or not placed_ts:
            continue
        age_hours = (now_ts - placed_ts) / 3600
        if age_hours < STALE_ORDER_HOURS:
            continue
        if dry_run:
            log_fn(f"  WOULD cancel stale order {txid} ({content.get('pair', '?')}, {age_hours:.1f}h old)")
        else:
            result = fetch(f"/api/orders/{txid}", method="DELETE")
            if "error" in result:
                log_fn(f"  Stale order {txid}: already filled or cancelled")
            else:
                log_fn(f"  Cancelled stale order {txid} ({age_hours:.1f}h old)")


def check_exits(
    holdings: list[dict], analyses: list[dict], stabilities: dict[str, float],
) -> list[dict]:
    """Check held positions for exit signals. Returns at most 1 exit order (worst first)."""
    exits: list[dict] = []
    for h in holdings:
        asset = h["asset"]
        if asset in QUOTE_CURRENCIES or h["value_usd"] < 5.0:
            continue
        pair = f"{asset}/USD"
        analysis = next((a for a in analyses if a["pair"] == pair), None)
        if not analysis:
            analysis = analyze_pair(pair)
        if not analysis:
            continue
        stab = stabilities.get(asset, 0)
        hold = score_hold(analysis, stab)
        if hold < 0.20:
            exits.append({
                "pair": pair, "side": "sell", "asset": asset,
                "hold_score": round(hold, 3),
                "reason": "quality_collapse",
                "price": analysis["price"],
                "qty": h["qty"],
                "value_usd": h["value_usd"],
            })
    # Only exit worst position per cycle — avoid panic-selling
    exits.sort(key=lambda e: e["hold_score"])
    return exits[:1]


def find_dust_positions(
    open_positions: list[dict], tracked_assets: set[str], threshold_usd: float,
) -> list[dict]:
    """Identify dust roots: USD value below threshold, not actively tracked."""
    dust = []
    for pos in open_positions:
        asset = pos["asset"]
        if asset in ("USD", "USDT", "USDC"):  # can't sell a quote currency as dust
            continue
        qty = float(pos.get("quantity_total", 0))
        if qty <= 0:
            continue
        # Get actual price — don't guess
        usd_val = float(pos.get("usd_value", 0))
        if usd_val == 0:
            pair = f"{asset}/USD"
            ohlcv = fetch(f"/api/ohlcv/{pair.replace('/', '%2F')}?interval=60&count=1")
            bars = ohlcv.get("bars", [])
            if bars:
                usd_val = qty * float(bars[-1]["close"])
            else:
                continue  # can't price it — skip, don't guess
        if usd_val < threshold_usd and asset not in tracked_assets:
            dust.append({"asset": asset, "qty": qty, "usd_value": usd_val,
                         "node_id": pos.get("node_id", "?")})
    return dust


def sweep_dust(dust_positions: list[dict], dry_run: bool, log_fn) -> list[dict]:
    """Attempt to sell dust positions via limit orders. Returns list of results."""
    results = []
    for d in dust_positions:
        pair = f"{d['asset']}/USD"
        # Get current price for limit order
        ohlcv = fetch(f"/api/ohlcv/{pair.replace('/', '%2F')}?interval=60&count=1")
        bars = ohlcv.get("bars", [])
        if not bars:
            log_fn(f"  DUST SKIP: {d['asset']} — no price data for {pair}")
            results.append({"asset": d["asset"], "action": "skipped"})
            continue
        price = float(bars[-1]["close"])
        # Limit sell slightly below market to ensure fill while paying maker fees
        limit_price = round(price * 0.998, _price_decimals(price))
        if dry_run:
            log_fn(f"  WOULD SELL dust: {d['asset']} qty={d['qty']:.6f} (~${d['usd_value']:.2f}) via {pair} @ {limit_price}")
            results.append({"asset": d["asset"], "action": "dry_run"})
            continue
        order = {
            "pair": pair, "side": "sell", "order_type": "limit",
            "quantity": str(d["qty"]), "limit_price": str(limit_price),
        }
        result = fetch("/api/orders", method="POST", data=order)
        if "error" in result:
            log_fn(f"  DUST FAIL: {d['asset']} — {result['error']}")
            # Write memory so we know this dust is stuck
            fetch("/api/memory", method="POST", data={
                "category": "observation",
                "content": {"type": "stuck_dust", "asset": d["asset"],
                            "qty": d["qty"], "reason": result["error"]},
                "importance": 0.3,
            })
            results.append({"asset": d["asset"], "action": "failed", "error": result["error"]})
        else:
            log_fn(f"  DUST SOLD: {d['asset']} txid={result.get('txid', '?')}")
            results.append({"asset": d["asset"], "action": "sold", "txid": result.get("txid")})
    return results


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

    # Step 1b: Check pending orders from previous cycles
    check_pending_orders(log, dry_run)

    # === Step 2: Observe ===
    log("\n--- Step 2: Observe ---")

    # Ground truth: compute portfolio value from exchange balances + live prices
    portfolio_value, holdings = compute_portfolio_value()
    cash_usd = next((h["value_usd"] for h in holdings if h["asset"] == "USD"), 0.0)
    if portfolio_value == 0:
        # Fallback to bot's cached view
        balances = fetch("/api/balances")
        cash_usd = float(balances.get("cash_usd", 0))
        portfolio_value = cash_usd

    max_position_value = portfolio_value * MAX_POSITION_PCT
    dust_threshold = portfolio_value * DUST_THRESHOLD_PCT
    log(f"Portfolio: ${portfolio_value:.2f}  |  USD: ${cash_usd:.2f}  |  Max trade: ${max_position_value:.2f}")

    # Show all holdings
    for h in holdings:
        if h["value_usd"] >= 1.0:
            log(f"  {h['asset']:8s} ${h['value_usd']:>8.2f}  (qty={h['qty']:.4f} @ ${h['price_usd']:.4f})")

    tree = fetch("/api/rotation-tree")
    open_positions = [n for n in tree.get("nodes", []) if n.get("depth", 0) == 0 and n["status"] == "open"]
    log(f"Rotation tree: {len(open_positions)} roots (tree value: ${tree.get('total_portfolio_value_usd', '?')})")

    # Compute stability per held asset using real holdings
    asset_volumes = get_asset_volumes()
    stabilities: dict[str, float] = {}
    for h in holdings:
        asset = h["asset"]
        if h["value_usd"] < 1.0:
            continue
        vol = asset_volumes.get(asset, 0)
        # Quick volatility estimate from regime endpoint
        pair_for_vol = f"{asset}/USD"
        regime_resp = fetch(f"/api/regime/{pair_for_vol.replace('/', '%2F')}?interval=60&count=300")
        vol_pct = 5.0  # default moderate volatility
        if "error" not in regime_resp and regime_resp.get("regime") == "volatile":
            vol_pct = 8.0
        elif "error" not in regime_resp and regime_resp.get("regime") == "trending":
            vol_pct = 3.0
        stabilities[asset] = compute_stability(vol, vol_pct)

    # === Step 3: Analyze (two-pass) ===
    log("\n--- Step 3: Analyze ---")
    all_discovered = discover_all_pairs(limit=40)
    # For now, prioritize USD-quoted pairs (most liquid) but include cross-pairs
    pairs_to_scan = [p["pair"] for p in all_discovered]
    log(f"  Discovered {len(pairs_to_scan)} liquid pairs")

    # Pass 1: quick regime check — filter out dead pairs
    viable: list[tuple[str, float]] = []
    for pair in pairs_to_scan:
        enc = pair.replace("/", "%2F")
        regime_data = fetch(f"/api/regime/{enc}?interval=60&count=300")
        if "error" in regime_data:
            continue
        gate = regime_data.get("trade_gate", 0)
        if gate >= MIN_REGIME_GATE:
            viable.append((pair, gate))
    log(f"  Pass 1: {len(viable)}/{len(pairs_to_scan)} pairs above regime floor ({MIN_REGIME_GATE})")

    # Pass 2: full analysis on viable pairs (cap at 15 to limit Kronos GPU time)
    analyses: list[dict] = []
    for pair, _ in viable[:15]:
        analysis = analyze_pair(pair)
        if analysis:
            score, bd = score_entry(analysis)
            analysis["_score"] = score
            analysis["_breakdown"] = bd
            analyses.append(analysis)
            regime_sym = {"trending": "T", "ranging": "R", "volatile": "V"}.get(analysis["regime"], "?")
            bd_str = " ".join(f"{k}={v:+.2f}" for k, v in bd.items() if isinstance(v, (int, float)))
            tfm = analysis.get("timesfm_direction", "?")[:4]
            log(f"  {pair:10s} {regime_sym} "
                f"gate={analysis['trade_gate']:.2f} "
                f"RSI={analysis['rsi_1h']:5.1f} "
                f"4H={analysis['trend_4h']:4s} "
                f"K={analysis['kronos_direction'][:4]:4s} "
                f"TFM={tfm:4s} "
                f"=> {score:.2f} [{bd_str}]")

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

    # 4a: Immediate post-mortem on losing trades
    losers = [t for t in recent_trades if float(t.get("net_pnl", 0)) < 0]
    if losers:
        immediate_postmortem(losers, log)

    # 4b: Deep post-mortem (every 72h) — aggregate patterns, write report
    deep_postmortem(log)

    # 4c: Self-tune parameters based on post-mortem patterns
    self_tune(recent_trades, analyses, log)

    # === Step 5: Decide ===
    log("\n--- Step 5: Decide ---")
    orders_to_place: list[dict] = []

    # 5a: Evaluate rotations — should any held position rotate to something better?
    proposals = evaluate_portfolio(open_positions, analyses, stabilities, all_discovered)
    if proposals:
        best_rot = proposals[0]
        log(f"ROTATION: {best_rot['from_asset']} -> {best_rot['to_asset']} via {best_rot['pair']} "
            f"(improvement={best_rot['improvement']:+.3f}, hold={best_rot['source_hold_score']:.2f}, "
            f"target={best_rot['target_score']:.2f}, threshold={best_rot['threshold']:.2f})")
        # Build the rotation order
        price = next((a["price"] for a in analyses if a["pair"] == best_rot["pair"]), None)
        if price:
            pos_for_rot = next((p for p in open_positions if p["asset"] == best_rot["from_asset"]), None)
            rot_value = min(max_position_value, float(pos_for_rot["quantity_total"]) * price) if pos_for_rot else max_position_value
            qty = round(rot_value / price, 6)
            limit_price = round(price * (1.002 if best_rot["side"] == "buy" else 0.998), _price_decimals(price))
            orders_to_place.append({
                "pair": best_rot["pair"], "side": best_rot["side"], "order_type": "limit",
                "quantity": str(qty), "limit_price": str(limit_price),
            })
    else:
        log("  No rotation opportunities above threshold.")

    # 5b: Deploy idle USD into best entry (if no rotation was found)
    if not orders_to_place:
        scored = [(a, a["_score"], a["_breakdown"]) for a in analyses]
        scored.sort(key=lambda x: -x[1])
        if scored and scored[0][1] > ENTRY_THRESHOLD and cash_usd >= max_position_value:
            best, score, bd = scored[0]
            bd_str = " ".join(f"{k}={v:+.2f}" for k, v in bd.items() if isinstance(v, (int, float)))
            log(f"ENTRY from USD: {best['pair']} score={score:.2f} [{bd_str}]")
            qty = round(max_position_value / best["price"], 6)
            limit_price = round(best["price"] * 1.002, _price_decimals(best["price"]))
            orders_to_place.append({
                "pair": best["pair"], "side": "buy", "order_type": "limit",
                "quantity": str(qty), "limit_price": str(limit_price),
            })
        else:
            top_reason = "no USD" if cash_usd < max_position_value else (
                f"best score={scored[0][1]:.2f}" if scored else "no data")
            log(f"  No entry: {top_reason}. Sitting out.")

    # 5c: Check exits — should any held position be sold?
    if not orders_to_place:
        exit_orders = check_exits(holdings, analyses, stabilities)
        if exit_orders:
            ex = exit_orders[0]
            log(f"EXIT: {ex['asset']} via {ex['pair']} — hold_score={ex['hold_score']:.2f} "
                f"(${ex['value_usd']:.2f}, reason={ex['reason']})")
            limit_price = round(ex["price"] * 0.998, _price_decimals(ex["price"]))
            orders_to_place.append({
                "pair": ex["pair"], "side": "sell", "order_type": "limit",
                "quantity": str(round(ex["qty"], 6)), "limit_price": str(limit_price),
            })

    # === Step 6: Act ===
    log("\n--- Step 6: Act ---")
    placed_txids: list[dict] = []
    if dry_run:
        log("DRY RUN — no orders placed")
        for order in orders_to_place:
            log(f"  WOULD: {order['side']} {order['quantity']} {order['pair']} @ {order.get('limit_price', 'market')}")
    else:
        for order in orders_to_place:
            result = fetch("/api/orders", method="POST", data=order)
            if "error" in result:
                log(f"  FAILED: {order['pair']} — {result['error']}")
            else:
                txid = result.get("txid", "?")
                log(f"  PLACED: {order['pair']} txid={txid}")
                placed_txids.append({"txid": txid, "pair": order["pair"],
                                      "side": order["side"], "placed_ts": time.time()})

    # Track placed orders in memory for fill monitoring
    for pt in placed_txids:
        fetch("/api/memory", method="POST", data={
            "category": "pending_order", "pair": pt["pair"],
            "content": pt, "importance": 0.6,
        })

    # Dust sweep — sell positions below 1% of portfolio
    tracked_assets = {p.split("/")[0] for p in TOP_PAIRS} | {p.split("/")[0] for p in pairs_to_scan}
    dust = find_dust_positions(open_positions, tracked_assets, dust_threshold)
    if dust:
        log(f"\n  Dust sweep: {len(dust)} position(s)")
        sweep_dust(dust, dry_run, log)
    else:
        log("  No dust to sweep.")

    # === Step 7: Remember ===
    log("\n--- Step 7: Remember ---")
    # Portfolio snapshot
    fetch("/api/memory", method="POST", data={
        "category": "portfolio_snapshot",
        "content": {"portfolio_value_usd": portfolio_value, "cash_usd": cash_usd,
                     "holdings_count": len([h for h in holdings if h["value_usd"] >= 1]),
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


LOOP_INTERVAL_SEC = 3600  # 1 hour between cycles


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    loop = "--loop" in sys.argv

    if not loop:
        run_brain(dry_run=dry_run)
        return

    print(f"CC Brain loop started — cycle every {LOOP_INTERVAL_SEC // 60} min, "
          f"{'DRY RUN' if dry_run else 'LIVE'}")
    while True:
        try:
            run_brain(dry_run=dry_run)
        except KeyboardInterrupt:
            print("\nBrain loop stopped by user.")
            break
        except Exception as exc:
            print(f"\n[ERROR] Brain cycle failed: {exc}")
            # Write error to memory so next cycle can see it
            try:
                fetch("/api/memory", method="POST", data={
                    "category": "observation",
                    "content": {"type": "brain_error", "error": str(exc)[:200]},
                    "importance": 0.9,
                })
            except Exception:
                pass
        # Sleep in 60s chunks so KeyboardInterrupt is responsive
        print(f"\nNext cycle in {LOOP_INTERVAL_SEC // 60} minutes...")
        try:
            for _ in range(LOOP_INTERVAL_SEC // 60):
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nBrain loop stopped by user.")
            break


if __name__ == "__main__":
    main()
