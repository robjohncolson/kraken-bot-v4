"""DOGE/USD decision snapshot CLI helper.

Spec 36 -- observation + journaling only, no trading actions.

Usage:
    python scripts/doge_snapshot.py [--bot-url URL] [--pair PAIR]
        [--log {DOGE,USD,SPLIT}] [--note STR] [--json] [--no-color]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _fetch(endpoint: str, bot_url: str, method: str = "GET", data: dict | None = None) -> dict:
    """Fetch JSON from the bot REST API.

    Returns {"error": "<msg>"} on any failure, never raises.
    Timeout 30s.
    """
    url = f"{bot_url}{endpoint}"
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
        try:
            body = json.loads(exc.read().decode())
            detail = body.get("detail", str(exc))
        except Exception:
            detail = str(exc)
        return {"error": detail}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Indicator helpers (stdlib only)
# ---------------------------------------------------------------------------

def _ema_series(values: list[float], span: int) -> list[float]:
    """Standard EMA with alpha = 2/(span+1). Seeded with values[0].

    Returns a list the same length as input.
    """
    if not values:
        return []
    alpha = 2.0 / (span + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * alpha + result[-1] * (1 - alpha))
    return result


def _rsi_wilder(closes: list[float], period: int = 14) -> float:
    """Wilder's RSI.

    First avg_gain/avg_loss: SMA over first `period` deltas.
    Subsequent: (prev * (period-1) + new) / period.
    Returns float in [0, 100]. Returns 50.0 if not enough data.
    """
    if len(closes) < period + 1:
        return 50.0

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    gains = [max(0.0, d) for d in deltas]
    losses = [max(0.0, -d) for d in deltas]

    # Seed with SMA of first period values
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder's smoothing for the rest
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        if avg_gain == 0:
            return 50.0
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _macd(closes: list[float], fast: int = 12, slow: int = 26,
          signal: int = 9) -> dict:
    """Compute MACD as full series.

    Returns {"line": [...], "signal": [...], "hist": [...]},
    each list the same length as `closes`.
    """
    fast_ema = _ema_series(closes, fast)
    slow_ema = _ema_series(closes, slow)
    line = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_line = _ema_series(line, signal)
    hist = [l - s for l, s in zip(line, signal_line)]
    return {"line": line, "signal": signal_line, "hist": hist}


def _hist_run(hist: list[float]) -> tuple[int, str]:
    """Return (count, color) for the trailing run of same-sign histogram bars.

    color is "g" for positive, "r" for negative, "-" if latest is zero or list empty.
    """
    if not hist:
        return (0, "-")
    latest = hist[-1]
    if latest == 0:
        return (0, "-")
    sign = 1 if latest > 0 else -1
    count = 0
    for v in reversed(hist):
        if (v > 0 and sign == 1) or (v < 0 and sign == -1):
            count += 1
        else:
            break
    color = "g" if sign == 1 else "r"
    return (count, color)


def _macd_cross(line: list[float], signal_line: list[float]) -> str:
    """Detect a MACD crossover on the last two bars.

    Returns "up", "down", or "none".
    """
    if len(line) < 2 or len(signal_line) < 2:
        return "none"
    prev_line = line[-2]
    curr_line = line[-1]
    prev_sig = signal_line[-2]
    curr_sig = signal_line[-1]
    if prev_line <= prev_sig and curr_line > curr_sig:
        return "up"
    if prev_line >= prev_sig and curr_line < curr_sig:
        return "down"
    return "none"


def _volatility_pct(closes: list[float], window: int = 14) -> float:
    """Rolling stdev of log returns over last `window` returns, as percentage.

    Returns 0.0 if fewer than window+1 closes.
    """
    if len(closes) < window + 1:
        return 0.0
    tail = closes[-(window + 1):]
    log_returns = [math.log(tail[i] / tail[i - 1]) for i in range(1, len(tail))]
    if len(log_returns) < 2:
        return 0.0
    return statistics.stdev(log_returns) * 100.0


def _24h_change_pct(closes_1h: list[float]) -> float:
    """Return (closes_1h[-1] / closes_1h[-25] - 1) * 100.

    Uses 1h bars (24 bars ago = 24h prior).
    Returns 0.0 if fewer than 25 bars.
    """
    if len(closes_1h) < 25:
        return 0.0
    return (closes_1h[-1] / closes_1h[-25] - 1) * 100.0


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------

# Mapping from human-readable TF name to API interval integer
_TF_INTERVALS = [
    ("1m", 1),
    ("15m", 15),
    ("1h", 60),
    ("4h", 240),
    ("1d", 1440),
]


def build_snapshot(bot_url: str, pair: str) -> dict:
    """Fetch all data and compute indicators. Never raises.

    Returns the full snapshot dict with per-TF errors captured in "errors".
    """
    errors: list[str] = []
    timestamp_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Encode pair for URL (replace / with %2F)
    pair_encoded = pair.replace("/", "%2F")

    # Fetch balances
    holdings: dict | None = None
    price = 0.0
    bal_resp = _fetch("/api/exchange-balances", bot_url)
    if "error" in bal_resp:
        errors.append(f"balances: {bal_resp['error']}")
    else:
        # Extract DOGE and USD quantities
        base_asset = pair.split("/")[0]  # e.g. "DOGE"
        quote_asset = pair.split("/")[1]  # e.g. "USD"
        items = bal_resp.get("balances", [])
        doge_qty = 0.0
        usd_cash = 0.0
        try:
            for item in items:
                asset = item.get("asset", "")
                avail = float(item.get("available", 0) or 0)
                held = float(item.get("held", 0) or 0)
                total = avail + held
                if asset == base_asset:
                    doge_qty = total
                elif asset == quote_asset:
                    usd_cash = total
        except (TypeError, ValueError, KeyError) as exc:
            errors.append(f"balances: parse error: {exc}")
            holdings = None
        else:
            holdings = {
                "doge_qty": doge_qty,
                "doge_value_usd": 0.0,  # filled in after we have price
                "usd_cash": usd_cash,
                "doge_pct": 0.0,
                "usd_pct": 0.0,
            }

    # Fetch OHLCV per timeframe
    timeframes: dict = {}
    closes_1h: list[float] = []

    for tf_name, interval in _TF_INTERVALS:
        endpoint = f"/api/ohlcv/{pair_encoded}?interval={interval}&count=200"
        resp = _fetch(endpoint, bot_url)
        if "error" in resp:
            errors.append(f"{tf_name}: {resp['error']}")
            timeframes[tf_name] = None
            continue

        bars = resp.get("bars", [])
        if not bars:
            errors.append(f"{tf_name}: empty bars")
            timeframes[tf_name] = None
            continue

        try:
            closes = [float(b["close"]) for b in bars]
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"{tf_name}: bad bar data: {exc}")
            timeframes[tf_name] = None
            continue

        if len(closes) < 35:
            errors.append(f"{tf_name}: insufficient bars ({len(closes)} < 35)")
            timeframes[tf_name] = None
            continue

        if tf_name == "1h":
            closes_1h = closes
            if closes:
                price = closes[-1]

        rsi = _rsi_wilder(closes)
        macd_data = _macd(closes)
        macd_line = macd_data["line"]
        macd_signal = macd_data["signal"]
        macd_hist = macd_data["hist"]

        hist_run_count, hist_color = _hist_run(macd_hist)
        cross = _macd_cross(macd_line, macd_signal)
        vol = _volatility_pct(closes)

        timeframes[tf_name] = {
            "rsi": round(rsi, 1),
            "macd_line": macd_line[-1] if macd_line else 0.0,
            "macd_signal": macd_signal[-1] if macd_signal else 0.0,
            "macd_cross": cross,
            "hist_run": hist_run_count,
            "hist_color": hist_color,
            "vol_pct": round(vol, 2),
            "bar_count": len(closes),
        }

    # If we got price from 1h, fill in holdings values
    if holdings is not None and price > 0:
        doge_value = holdings["doge_qty"] * price
        total = doge_value + holdings["usd_cash"]
        holdings["doge_value_usd"] = doge_value
        if total > 0:
            holdings["doge_pct"] = round(doge_value / total * 100, 1)
            holdings["usd_pct"] = round(holdings["usd_cash"] / total * 100, 1)

    # 24h change from 1h bars
    change_pct = _24h_change_pct(closes_1h)
    if change_pct > 0.05:
        color_24h = "green"
    elif change_pct < -0.05:
        color_24h = "red"
    else:
        color_24h = "flat"

    return {
        "pair": pair,
        "timestamp_utc": timestamp_utc,
        "price": price,
        "change_24h_pct": round(change_pct, 4),
        "change_24h_color": color_24h,
        "holdings": holdings,
        "timeframes": timeframes,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_RESET = "\x1b[0m"


def render_human(snapshot: dict, *, color: bool) -> str:
    """Render snapshot as a human-readable terminal string."""
    lines: list[str] = []

    # Header
    ts = snapshot.get("timestamp_utc", "")
    # Convert ISO format if needed
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        ts_display = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        ts_display = ts
    lines.append(f"{snapshot.get('pair', '?')}  -  {ts_display}")

    # Price + 24h
    price = snapshot.get("price", 0.0)
    chg = snapshot.get("change_24h_pct", 0.0)
    chg_color = snapshot.get("change_24h_color", "flat")
    sign = "+" if chg >= 0 else ""
    chg_str = f"{sign}{chg:.2f}%"
    color_label = chg_color.upper()

    if color:
        if chg_color == "green":
            color_label = f"{_GREEN}{color_label}{_RESET}"
            chg_str = f"{_GREEN}{chg_str}{_RESET}"
        elif chg_color == "red":
            color_label = f"{_RED}{color_label}{_RESET}"
            chg_str = f"{_RED}{chg_str}{_RESET}"

    lines.append(f"Price: ${price:.5f}    24h: {chg_str}  {color_label}")

    # Holdings
    h = snapshot.get("holdings")
    if h is None:
        lines.append("Holdings: unavailable")
    else:
        doge_qty = h.get("doge_qty", 0.0)
        doge_val = h.get("doge_value_usd", 0.0)
        usd_cash = h.get("usd_cash", 0.0)
        doge_pct = h.get("doge_pct", 0.0)
        usd_pct = h.get("usd_pct", 0.0)
        pair = snapshot.get("pair", "DOGE/USD")
        base = pair.split("/")[0]
        lines.append(
            f"Holdings: {doge_qty:.2f} {base} (${doge_val:.2f})  |  "
            f"${usd_cash:.2f} USD  |  {doge_pct:.0f}% / {usd_pct:.0f}%"
        )

    lines.append("")

    # TF table header
    lines.append(f"{'TF':<6}  {'RSI':<5}  {'MACD line':<10}  {'Cross':<6}  {'Hist':<7}  {'Vol%':<5}")
    lines.append(f"{'------':<6}  {'----':<5}  {'---------':<10}  {'-----':<6}  {'------':<7}  {'-----':<5}")

    tf_order = ["1m", "15m", "1h", "4h", "1d"]
    for tf in tf_order:
        entry = snapshot.get("timeframes", {}).get(tf)
        if entry is None:
            lines.append(f"{tf:<6}  FETCH FAILED  --  --  --  --")
            continue

        rsi = entry.get("rsi", 0.0)
        macd_line_val = entry.get("macd_line", 0.0)
        cross = entry.get("macd_cross", "none")
        hist_run = entry.get("hist_run", 0)
        hist_color_val = entry.get("hist_color", "-")
        vol = entry.get("vol_pct", 0.0)

        # Cross symbol
        if cross == "up":
            cross_sym = "^"
            cross_colored = f"{_GREEN}^{_RESET}" if color else "^"
        elif cross == "down":
            cross_sym = "v"
            cross_colored = f"{_RED}v{_RESET}" if color else "v"
        else:
            cross_sym = "-"
            cross_colored = "-"

        # Hist cell
        if hist_color_val == "g":
            hist_cell = f"+{hist_run}g"
            hist_colored = f"{_GREEN}+{hist_run}g{_RESET}" if color else hist_cell
        elif hist_color_val == "r":
            hist_cell = f"-{hist_run}r"
            hist_colored = f"{_RED}-{hist_run}r{_RESET}" if color else hist_cell
        else:
            hist_cell = "0-"
            hist_colored = hist_cell

        macd_sign = "+" if macd_line_val >= 0 else ""
        macd_str = f"{macd_sign}{macd_line_val:.6f}"

        if color:
            cross_display = cross_colored
            hist_display = hist_colored
        else:
            cross_display = cross_sym
            hist_display = hist_cell

        lines.append(
            f"{tf:<6}  {rsi:<5.1f}  {macd_str:<10}  {cross_display:<6}  {hist_display:<7}  {vol:<5.2f}"
        )

    return "\n".join(lines)


def render_json(snapshot: dict) -> str:
    """Return JSON representation of the snapshot."""
    return json.dumps(snapshot, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

_VALID_DECISIONS = {"DOGE", "USD", "SPLIT"}


def log_decision(bot_url: str, snapshot: dict, decision: str,
                 note: str | None) -> dict:
    """POST snapshot + decision to cc_memory as a doge_snapshot row.

    Raises ValueError if decision is not in {"DOGE", "USD", "SPLIT"}.
    Returns the parsed response dict.
    """
    if decision not in _VALID_DECISIONS:
        raise ValueError(
            f"Invalid decision {decision!r}. Must be one of {sorted(_VALID_DECISIONS)}"
        )

    payload = {
        "category": "doge_snapshot",
        "pair": snapshot.get("pair", "DOGE/USD"),
        "importance": 0.7,
        "content": {
            "snapshot": snapshot,
            "decision": decision,
            "note": note,
            "schema_version": "v1",
        },
    }
    return _fetch("/api/memory", bot_url, method="POST", data=payload)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="DOGE/USD decision snapshot helper (Spec 36)"
    )
    parser.add_argument("--bot-url", default="http://127.0.0.1:58392",
                        help="Bot base URL (default: http://127.0.0.1:58392)")
    parser.add_argument("--pair", default="DOGE/USD",
                        help="Trading pair (default: DOGE/USD)")
    parser.add_argument("--log", choices=["DOGE", "USD", "SPLIT"],
                        help="Log a decision to cc_memory after rendering")
    parser.add_argument("--note", default=None,
                        help="Free-text note attached to logged row (requires --log)")
    parser.add_argument("--json", action="store_true", dest="emit_json",
                        help="Emit JSON instead of human view")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable ANSI color escapes")

    args = parser.parse_args(argv)

    if args.pair != "DOGE/USD":
        print(f"error: --pair {args.pair!r} not supported (v1 is DOGE/USD only)", file=sys.stderr)
        sys.exit(2)

    snapshot = build_snapshot(args.bot_url, args.pair)

    if args.emit_json:
        print(render_json(snapshot))
        sys.exit(0)

    print(render_human(snapshot, color=not args.no_color))

    # Exit 1 if every TF failed -- and skip the log step too
    tf = snapshot.get("timeframes", {})
    if not tf or all(v is None for v in tf.values()):
        if args.log:
            print("log skipped: no timeframes populated")
        sys.exit(1)

    if args.log:
        try:
            resp = log_decision(args.bot_url, snapshot, args.log, args.note)
            if "error" in resp:
                print(f"log failed: {resp['error']}")
                sys.exit(1)
            mem_id = resp.get("id", 0)
            if not isinstance(mem_id, int) or mem_id <= 0:
                print(f"log failed: memory write returned id={mem_id!r}")
                sys.exit(1)
            print(f"logged: {args.log}")
        except ValueError as exc:
            print(f"error: {exc}")
            sys.exit(1)


if __name__ == "__main__":
    main()
