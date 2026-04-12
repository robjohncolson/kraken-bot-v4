# Plan 07 — Ordermin + costmin pre-check

## File targets

- `scripts/cc_brain.py` — only file to modify

## Dependency

This is independent of the other hardening specs. Can run in parallel
with specs 06, 08, 09, 10 (all touch different parts of cc_brain.py or
different files).

## Step-by-step

### Step 1: Store ordermin and costmin in the pair cache

In `_fetch_kraken_pairs()` (around line 97), the current dict creation is:

```python
all_pairs[f"{base}/{quote}"] = {
    "key": key, "base": base, "quote": quote,
    "pair_decimals": meta.get("pair_decimals"),
    "lot_decimals": meta.get("lot_decimals"),
}
```

Extend to:

```python
all_pairs[f"{base}/{quote}"] = {
    "key": key, "base": base, "quote": quote,
    "pair_decimals": meta.get("pair_decimals"),
    "lot_decimals": meta.get("lot_decimals"),
    "ordermin": float(meta["ordermin"]) if meta.get("ordermin") else None,
    "costmin": float(meta["costmin"]) if meta.get("costmin") else None,
}
```

### Step 2: Add the helper

Near `_floor_qty` and `_price_decimals`, add:

```python
def _meets_order_minimums(pair: str, qty: float, price: float) -> tuple[bool, str]:
    """Check if (qty, price) clears Kraken's ordermin and costmin for the pair.

    Returns (True, "") if OK, (False, reason) otherwise. If the pair is
    unknown, returns (True, "") — we can't block on unknown minimums.
    """
    try:
        pairs = _fetch_kraken_pairs()
    except Exception:
        return True, ""
    info = pairs.get(pair)
    if not info:
        return True, ""
    ordermin = info.get("ordermin")
    costmin = info.get("costmin")
    if ordermin is not None and qty < ordermin:
        return False, f"qty {qty:.6f} < ordermin {ordermin}"
    cost = qty * price
    if costmin is not None and cost < costmin:
        return False, f"cost ${cost:.2f} < costmin ${costmin}"
    return True, ""
```

### Step 3: Filter entry candidates in Step 5b

Find the entry decision block:

```python
if not orders_to_place:
    scored = [(a, a["_score"], a["_breakdown"]) for a in analyses
              if a["pair"] not in pending_pairs]
    scored.sort(key=lambda x: -x[1])
    if scored and scored[0][1] > ENTRY_THRESHOLD and cash_usd >= max_position_value:
        ...
```

Before the `if scored and ...` line, insert a filter pass:

```python
    # Filter out pairs whose computed qty would fail Kraken's minimums.
    filtered_scored = []
    for a, s, bd in scored:
        price = a["price"]
        candidate_qty = max_position_value / price if price > 0 else 0
        ok, reason = _meets_order_minimums(a["pair"], candidate_qty, price)
        if ok:
            filtered_scored.append((a, s, bd))
        else:
            log(f"  Skip {a['pair']} (score={s:.2f}): {reason}")
    scored = filtered_scored
```

### Step 4: Filter rotation proposals in evaluate_portfolio

In `evaluate_portfolio()`, right before appending to proposals, check
the outgoing sell quantity against minimums:

```python
for to_asset, to_analysis in analysis_by_base.items():
    if to_asset == from_asset:
        continue
    if to_asset in QUOTE_CURRENCIES or to_asset in FIAT_CURRENCIES:
        continue
    to_score = to_analysis["_score"]
    improvement = to_score - hold_score
    if improvement < threshold:
        continue

    pair_info = pair_lookup.get((from_asset, to_asset)) or pair_lookup.get((to_asset, from_asset))
    if not pair_info:
        continue

    pair = pair_info["pair"]
    side = "sell" if pair_info["base"] == from_asset else "buy"

    # Ordermin check — use from_pos qty for sell, computed qty for buy.
    if side == "sell":
        check_qty = float(pos.get("quantity_total", 0))
        check_price = to_analysis.get("price", 0)
    else:
        check_qty = (float(pos.get("usd_value", 0)) / to_analysis["price"]
                     if to_analysis.get("price") else 0)
        check_price = to_analysis["price"]
    ok, _ = _meets_order_minimums(pair, check_qty, check_price)
    if not ok:
        continue  # silently skip — rotation churn would spam logs

    proposals.append({
        ...
    })
```

Note: the rotation filter is SILENT (no log) because rotation evaluation
scans many candidates and logging each skip would be noise.

### Step 5: Filter exits in check_exits

In `check_exits()`, right after building the exit dict, check minimums:

```python
if hold < 0.20:
    exit_order = {
        "pair": pair, "side": "sell", "asset": asset,
        "hold_score": round(hold, 3),
        "reason": "quality_collapse",
        "price": analysis["price"],
        "qty": h["qty"],
        "value_usd": h["value_usd"],
    }
    ok, reason = _meets_order_minimums(pair, h["qty"], analysis["price"])
    if not ok:
        # Position too small to sell — skip (mark as stuck dust path).
        # Log once per cycle so we can see this in the reports.
        continue  # the calling log_fn is not available here; handled in run_brain
    exits.append(exit_order)
```

Actually check_exits doesn't have log_fn in scope. Log the skip at the
call site in run_brain instead: track a separate list of "stuck" assets
and log them in one line after check_exits returns.

Actually simplest: just skip silently in check_exits. The calling log
already shows all held assets in Step 2; the operator can see which
positions aren't being exited.

## Testing

1. **Syntax check**
2. **Verify pair cache has ordermin**:
   ```python
   from scripts.cc_brain import _fetch_kraken_pairs
   p = _fetch_kraken_pairs()
   print(p.get("RAVE/USD", {}).get("ordermin"))
   # Should print a number, not None
   ```
3. **Verify _meets_order_minimums**:
   ```python
   from scripts.cc_brain import _meets_order_minimums
   # Simulated tiny-budget RAVE entry (would fail)
   ok, reason = _meets_order_minimums("RAVE/USD", 1.0, 0.01)
   print(ok, reason)  # Expect False + reason
   ```
4. **Dry-run cycle**: run `python scripts/cc_brain.py --dry-run` and
   look for a `Skip X/USD (score=Y): qty Z < ordermin N` log line, if
   any pair fails. If no pair fails, that's fine too — it means all
   top candidates clear minimums naturally.

## Rollback

`git revert` the commit. The new helper is purely additive; removing
the filter calls restores pre-fix behavior.

## Commit message

```
Pre-check ordermin + costmin before proposing orders

Previously, cc_brain would score a pair, pick it as the top candidate,
build an order, and get rejected by Kraken when the computed qty was
below the pair's ordermin. Most visible example: RAVE/USD on
2026-04-12 01:32 UTC — proposal passed all scoring and was rejected
with "volume minimum not met".

_fetch_kraken_pairs now caches ordermin + costmin from AssetPairs.
New _meets_order_minimums helper consults the cache. Three call sites
filter candidates through it:

1. Step 5b entry scoring — pairs whose max_position budget can't meet
   ordermin are dropped from the scored list with a log.
2. evaluate_portfolio rotation — silently skips rotation proposals
   whose from-side qty is below ordermin (avoids churn).
3. check_exits — skips exit proposals for positions too small to
   liquidate (they fall through to the dust path).

Addresses the stuck retry loop flagged in feedback memory
"Kraken minimum order sizes".
```
