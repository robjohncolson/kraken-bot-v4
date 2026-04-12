# Plan 01 — Floor-round exit quantities

## File targets

- `scripts/cc_brain.py` — the only file to modify

## Functions affected

1. `sweep_dust()` — builds sell orders for dust positions (~line 985)
2. Step 5a rotation code — builds rotation orders, some are sells (~line 1210)
3. Step 5c exit code — builds exit sell orders (~line 1250)

## Step-by-step

### Step 1: Add a helper

Near the top of `scripts/cc_brain.py`, right after `_price_decimals`
(around line 55), add:

```python
def _floor_qty(qty: float, pair: str | None = None) -> str:
    """Floor-round a SELL quantity to the pair's lot_decimals.

    Kraken rejects sells whose volume exceeds actual available balance,
    even by 1e-7. Round-nearest can tip over the edge; floor cannot.
    Returns a string suitable for the Kraken volume field.
    """
    import math
    decimals = 6  # fallback
    if pair:
        try:
            pairs = _fetch_kraken_pairs()
            info = pairs.get(pair)
            if info and info.get("lot_decimals") is not None:
                decimals = int(info["lot_decimals"])
        except Exception:
            pass
    factor = 10 ** decimals
    floored = math.floor(qty * factor) / factor
    # Format without trailing zeros but preserve precision.
    return f"{floored:.{decimals}f}".rstrip("0").rstrip(".") or "0"
```

Note: lot_decimals on Kraken for crypto assets is usually 8; the
floor-rounding still correctly eliminates the 10th-decimal overflow
we've been hitting.

### Step 2: Update sweep_dust (sell side)

Find around line 985:
```python
order = {
    ...
    "quantity": str(round(d["qty"], 6)),
    ...
}
```

Change to:
```python
order = {
    ...
    "quantity": _floor_qty(d["qty"], pair),
    ...
}
```

### Step 3: Update step 5a rotation (sell side only)

Find around line 1205-1215 where a rotation order is built:
```python
orders_to_place.append({
    "pair": best_rot["pair"], "side": best_rot["side"], "order_type": "limit",
    "quantity": str(qty), "limit_price": str(limit_price),
})
```

Replace `str(qty)` with:
```python
"quantity": (_floor_qty(float(qty), best_rot["pair"])
             if best_rot["side"] == "sell" else str(qty)),
```

Only floor on sell side — buy side is derived from USD budget, not
from owned balance.

### Step 4: Update step 5c exit

Find around line 1250:
```python
orders_to_place.append({
    "pair": ex["pair"], "side": "sell", "order_type": "limit",
    "quantity": str(round(ex["qty"], 6)), "limit_price": str(limit_price),
})
```

Replace with:
```python
orders_to_place.append({
    "pair": ex["pair"], "side": "sell", "order_type": "limit",
    "quantity": _floor_qty(ex["qty"], ex["pair"]),
    "limit_price": str(limit_price),
})
```

## Testing

1. **Syntax check:** `python -c "import ast; ast.parse(open('scripts/cc_brain.py').read())"`
2. **Unit-style check** — add a brief test at the bottom of the file or
   in a tests/ module:
   ```python
   # Verify floor always rounds down
   assert _floor_qty(91.5370539700, "CRV/USD") <= "91.5370539700"
   assert float(_floor_qty(91.5370539700, "CRV/USD")) <= 91.5370539700
   ```
3. **Dry-run:** `python scripts/cc_brain.py --dry-run` and verify the
   log shows a sell quantity ≤ actual available balance. The balance
   comparison can be eyeballed from `/api/exchange-balances`.
4. **Live run:** `python scripts/cc_brain.py` (no --dry-run) and
   verify the CRV exit either succeeds with `PLACED` or, if Kraken
   now returns a different error, that error is NOT
   `EOrder:Insufficient funds`.

## Rollback

`git revert` the commit. No schema changes, no state migration.

## Commit message

```
Floor-round sell quantities to avoid insufficient-funds rejections

The bot was rounding exit quantities to 6 decimals with round-nearest,
which could produce a volume exceeding actual Kraken balance by up to
1e-6. Kraken rejected these as insufficient funds even when balance
was available to within 1e-7.

New _floor_qty(qty, pair) helper uses lot_decimals from AssetPairs
(or 6-decimal fallback) and rounds DOWN. Applied to all three sell
paths: sweep_dust, step 5a rotation (sell side), step 5c exit.

Verified by checking CRV/COMP exits no longer emit
EOrder:Insufficient funds.
```
