# Plan 34 -- Premature exit detector

## Implementation steps

### Step 1: Create `analysis/` package

- `analysis/__init__.py` -- empty file
- `analysis/premature_exit.py` -- main module

### Step 2: Module structure

```python
"""Premature-exit detector (Qullamaggie rule).

Scans closed trades in trade_outcomes, reconstructs 4H EMA(10)/EMA(20)
at exit time from CryptoCompare historical bars, and tags exits where
price was still above both MAs as 'premature' in cc_memory.

Detection only -- does not change exit logic.
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
from typing import Iterable

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
    """Iterative EMA identical to scripts/cc_brain.compute_ema."""
    m = 2 / (span + 1)
    e = values[0]
    for v in values[1:]:
        e = v * m + e * (1 - m)
    return e


def _classify(
    exit_price: Decimal,
    ema10: Decimal,
    ema20: Decimal,
    exit_reason: str,
) -> bool:
    """Return True iff the exit is premature by the v1 rule."""
    if exit_reason in EXCLUDED_REASONS:
        return False
    return exit_price > ema10 and exit_price > ema20


def _aggregate_1h_to_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Resample 1H bars into 4H bars on UTC boundaries (00/04/08/12/16/20).

    Drops any bar whose group does not contain all 4 constituent 1H bars
    (so the returned DataFrame contains only fully-closed 4H bars).
    """
    if df_1h.empty:
        return df_1h
    # Build a DatetimeIndex from the 'timestamp' column (unix seconds).
    df = df_1h.copy()
    df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df.set_index("dt").sort_index()
    # Resample with origin at UTC midnight so buckets align to 00/04/08/12/16/20.
    resampled = df.resample("4H", origin="epoch").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        _n=("open", "size"),
    )
    # Keep only complete buckets (4 constituent 1H bars).
    complete = resampled[resampled["_n"] == 4].drop(columns=["_n"])
    complete = complete.reset_index(drop=False)
    complete = complete.rename(columns={"dt": "bucket_start"})
    return complete


def _fetch_trade_outcomes(bot_url: str, lookback_days: int) -> list[dict]:
    url = f"{bot_url}/api/trade-outcomes?lookback_days={lookback_days}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return data.get("outcomes", [])


def _already_flagged_ids(cc_memory: CCMemory) -> set[int]:
    """Read all existing premature_exit memories and collect their ids."""
    rows = cc_memory.query(
        category="premature_exit",
        hours=24 * 365 * 10,
        limit=100000,
    )
    ids: set[int] = set()
    for r in rows:
        try:
            content = r.get("content") or {}
            if isinstance(content, str):
                content = json.loads(content)
            tid = content.get("trade_outcome_id")
            if tid is not None:
                ids.add(int(tid))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return ids


def detect_premature_exits(
    lookback_days: int,
    cc_memory: CCMemory,
    trade_outcomes: list[dict],
    *,
    dry_run: bool = False,
    fetch_bars=fetch_ohlcv_cryptocompare,  # injectable for tests
) -> dict:
    """Classify each exit and write premature_exit memories.

    Returns {"scanned", "flagged", "skipped", "errors"}.
    """
    scanned = flagged = skipped = errors = 0
    already = _already_flagged_ids(cc_memory)

    for o in trade_outcomes:
        scanned += 1
        try:
            outcome_id = int(o["id"])
        except (KeyError, TypeError, ValueError):
            errors += 1
            continue
        if outcome_id in already:
            skipped += 1
            continue

        pair = o.get("pair", "")
        exit_reason = o.get("exit_reason", "")
        closed_at = o.get("closed_at", "")
        try:
            exit_price = Decimal(str(o["exit_price"]))
            net_pnl = Decimal(str(o.get("net_pnl", "0")))
            exit_dt = datetime.fromisoformat(closed_at)
            if exit_dt.tzinfo is None:
                exit_dt = exit_dt.replace(tzinfo=timezone.utc)
            exit_ts = int(exit_dt.timestamp())
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Skipping outcome %s: parse error: %s", outcome_id, exc)
            errors += 1
            continue

        # Fetch 30 days of 1H history ending at exit time
        since_ts = exit_ts - 30 * 86400
        try:
            bars_1h = fetch_bars(
                pair, interval=60, since=since_ts, until=exit_ts
            )
        except CryptoCompareError as exc:
            logger.warning("Skipping outcome %s (%s): ohlcv fetch: %s",
                           outcome_id, pair, exc)
            errors += 1
            continue
        except Exception as exc:
            logger.warning("Skipping outcome %s (%s): unexpected: %s",
                           outcome_id, pair, exc)
            errors += 1
            continue

        bars_4h = _aggregate_1h_to_4h(bars_1h)
        # Use only bars whose bucket_start < exit_ts
        bars_4h = bars_4h[
            bars_4h["bucket_start"].astype("int64") // 10**9 < exit_ts
        ]
        if len(bars_4h) < MIN_4H_BARS_REQUIRED:
            skipped += 1
            continue

        closes = [float(c) for c in bars_4h["close"].tolist()]
        ema10 = Decimal(str(_compute_ema(closes, 10)))
        ema20 = Decimal(str(_compute_ema(closes, 20)))

        if _classify(exit_price, ema10, ema20, exit_reason):
            flagged += 1
            if not dry_run:
                cc_memory._write(
                    "premature_exit",
                    {
                        "trade_outcome_id": outcome_id,
                        "closed_at": closed_at,
                        "exit_reason": exit_reason,
                        "exit_price": str(exit_price),
                        "ema10_4h": str(ema10),
                        "ema20_4h": str(ema20),
                        "net_pnl": str(net_pnl),
                        "rule_version": RULE_VERSION,
                    },
                    pair=pair,
                    importance=0.7,
                )

    return {
        "scanned": scanned,
        "flagged": flagged,
        "skipped": skipped,
        "errors": errors,
    }


def main() -> int:
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
    sys.exit(main())
```

### Step 3: Wire into `scripts/cc_postmortem.py`

At the bottom of `main()`, after the report is written:

```python
    # Premature-exit detector (best-effort, non-fatal)
    try:
        from analysis.premature_exit import detect_premature_exits
        from persistence.cc_memory import CCMemory
        cc_memory = CCMemory("data/bot.db")
        result = detect_premature_exits(
            lookback_days=30,
            cc_memory=cc_memory,
            trade_outcomes=outcomes,  # reuse the list fetched above
        )
        print(
            f"[premature_exit] scanned={result['scanned']} "
            f"flagged={result['flagged']} skipped={result['skipped']} "
            f"errors={result['errors']}"
        )
    except Exception as exc:
        print(f"[premature_exit] detector error (non-fatal): {exc}")
```

Note: `run_postmortem` currently parses `outcomes` into `trades` (typed
dicts). We need the raw `outcomes` list for the detector. Either:
- Refactor `run_postmortem` to accept/return `outcomes` as well, OR
- Re-fetch `outcomes` in `main()` and pass both to the detector and the
  report function.

Pick the minimal change: re-fetch in `main()` -- it's one extra HTTP call
and keeps `run_postmortem`'s signature unchanged.

### Step 4: Create test file `tests/analysis/__init__.py` (empty)

### Step 5: Create `tests/analysis/test_premature_exit.py`

Nine tests per spec acceptance-criteria #7. Use `CCMemory(":memory:")` for
fresh state; monkeypatch `fetch_ohlcv_cryptocompare` via the `fetch_bars`
parameter of `detect_premature_exits`.

Sketch:

```python
from decimal import Decimal
import pandas as pd

import pytest

from analysis.premature_exit import (
    _aggregate_1h_to_4h,
    _classify,
    detect_premature_exits,
)
from persistence.cc_memory import CCMemory


def test_classify_premature_when_above_both_emas():
    assert _classify(
        Decimal("100"), Decimal("95"), Decimal("90"), "timer"
    ) is True


def test_classify_not_premature_when_below_ema10():
    assert _classify(
        Decimal("92"), Decimal("95"), Decimal("90"), "timer"
    ) is False


def test_classify_not_premature_when_stop_loss():
    assert _classify(
        Decimal("100"), Decimal("95"), Decimal("90"), "stop_loss"
    ) is False


def _mk_1h_bars(start_ts: int, n: int, *, open_=100, step=0):
    rows = []
    for i in range(n):
        price = open_ + i * step
        rows.append({
            "timestamp": start_ts + i * 3600,
            "open": Decimal(str(price)),
            "high": Decimal(str(price + 1)),
            "low": Decimal(str(price - 1)),
            "close": Decimal(str(price)),
            "volume": Decimal("1"),
        })
    return pd.DataFrame(rows)


def test_aggregate_1h_to_4h_boundary():
    # 8 bars starting at a 4H UTC boundary (00:00)
    start = 1_700_000_000 - (1_700_000_000 % (4 * 3600))
    df_1h = _mk_1h_bars(start, 8, open_=100, step=1)
    df_4h = _aggregate_1h_to_4h(df_1h)
    assert len(df_4h) == 2
    # first 4h bucket: opens 100, closes 103, high 104, low 99
    assert float(df_4h.iloc[0]["open"]) == 100
    assert float(df_4h.iloc[0]["close"]) == 103


def test_aggregate_1h_to_4h_drops_partial():
    start = 1_700_000_000 - (1_700_000_000 % (4 * 3600))
    df_1h = _mk_1h_bars(start, 10, open_=100, step=0)
    df_4h = _aggregate_1h_to_4h(df_1h)
    # 10 bars -> 2 complete 4H + 2 extra -> 2 complete buckets only
    assert len(df_4h) == 2


def _stub_fetch_returning(df_1h):
    def fetch(pair, interval=60, since=None, until=None, **_kw):
        return df_1h
    return fetch


def test_detect_writes_memory_for_premature_exit():
    # Synthetic: 120 1H bars all at price 100 -> 30 complete 4H bars
    # EMAs will converge to 100. Exit at 105 is above both EMAs.
    start = 1_700_000_000 - (1_700_000_000 % (4 * 3600))
    df = _mk_1h_bars(start, 120, open_=100, step=0)
    stub = _stub_fetch_returning(df)

    outcomes = [{
        "id": 1,
        "pair": "SOL/USD",
        "exit_price": "105",
        "net_pnl": "2.50",
        "exit_reason": "timer",
        "closed_at": "2023-11-15T00:00:00+00:00",
    }]
    cc = CCMemory(":memory:")
    r = detect_premature_exits(
        lookback_days=30, cc_memory=cc, trade_outcomes=outcomes,
        fetch_bars=stub,
    )
    assert r["flagged"] == 1
    rows = cc.query(category="premature_exit", hours=24*365, limit=100)
    assert len(rows) == 1


def test_detect_idempotent_on_rerun():
    start = 1_700_000_000 - (1_700_000_000 % (4 * 3600))
    df = _mk_1h_bars(start, 120, open_=100, step=0)
    stub = _stub_fetch_returning(df)
    outcomes = [{
        "id": 1, "pair": "SOL/USD", "exit_price": "105",
        "net_pnl": "2.50", "exit_reason": "timer",
        "closed_at": "2023-11-15T00:00:00+00:00",
    }]
    cc = CCMemory(":memory:")
    detect_premature_exits(30, cc, outcomes, fetch_bars=stub)
    r2 = detect_premature_exits(30, cc, outcomes, fetch_bars=stub)
    assert r2["flagged"] == 0
    assert r2["skipped"] == 1


def test_detect_skips_exits_with_insufficient_history():
    start = 1_700_000_000 - (1_700_000_000 % (4 * 3600))
    df = _mk_1h_bars(start, 20, open_=100, step=0)  # only 5 4H bars
    stub = _stub_fetch_returning(df)
    outcomes = [{
        "id": 1, "pair": "SOL/USD", "exit_price": "105",
        "net_pnl": "0", "exit_reason": "timer",
        "closed_at": "2023-11-15T00:00:00+00:00",
    }]
    cc = CCMemory(":memory:")
    r = detect_premature_exits(30, cc, outcomes, fetch_bars=stub)
    assert r["flagged"] == 0
    assert r["skipped"] == 1


def test_detect_continues_on_per_pair_error():
    from research.ohlcv_cryptocompare import CryptoCompareError
    start = 1_700_000_000 - (1_700_000_000 % (4 * 3600))
    good_df = _mk_1h_bars(start, 120, open_=100, step=0)

    def fetch(pair, interval=60, since=None, until=None, **_kw):
        if pair == "BAD/USD":
            raise CryptoCompareError("boom")
        return good_df

    outcomes = [
        {"id": 1, "pair": "BAD/USD", "exit_price": "100",
         "net_pnl": "0", "exit_reason": "timer",
         "closed_at": "2023-11-15T00:00:00+00:00"},
        {"id": 2, "pair": "SOL/USD", "exit_price": "105",
         "net_pnl": "0", "exit_reason": "timer",
         "closed_at": "2023-11-15T00:00:00+00:00"},
    ]
    cc = CCMemory(":memory:")
    r = detect_premature_exits(30, cc, outcomes, fetch_bars=fetch)
    assert r["errors"] == 1
    assert r["flagged"] == 1
```

### Step 6: Run pytest

```
C:/Python313/python.exe -m pytest tests/analysis -x -q
C:/Python313/python.exe -m pytest tests/ -x -q
```

Expect: all new tests green, total 697 pass (688 baseline + 9 new).

### Step 7: Manual smoke test (CC runs after Codex returns)

- `python analysis/premature_exit.py --dry-run` against live bot
- Then without `--dry-run`
- `sqlite3 data/bot.db "SELECT COUNT(*) FROM cc_memory WHERE category='premature_exit'"`

## Notes for Codex

- `pd.DataFrame.resample("4H", origin="epoch")` aligns buckets to unix
  epoch -- that gives us 00/04/08/12/16/20 UTC boundaries. Verify this
  in the test rather than trusting docs.
- `CCMemory._write` is prefixed with underscore but is the right entry
  point since there is no `record_premature_exit` helper yet. Do NOT
  add one -- keep the surface minimal.
- Re-fetch `outcomes` in `cc_postmortem.main()` rather than refactoring
  `run_postmortem`. It's simpler and the list is small.
- The `fetch_bars` injection parameter keeps tests offline. Do not
  replace it with `unittest.mock.patch` decorators.

## Owned paths

- `analysis/__init__.py`
- `analysis/premature_exit.py`
- `tests/analysis/__init__.py`
- `tests/analysis/test_premature_exit.py`
- `scripts/cc_postmortem.py`
