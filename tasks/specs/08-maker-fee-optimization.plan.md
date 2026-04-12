# Plan 08 — Maker-fee optimization

## File targets

- `scripts/cc_brain.py` — only file to modify

## Dependency

This depends on spec 01 being merged (floor-round). It does NOT depend
on 02, 03, 06, 07, 09, or 10 — can run in parallel with any of those.
For the manifest, mark it as depends_on `01-floor-round` which is
already merged to master.

## Step-by-step

### Step 1: Add the buffer constants

Near the other strategy parameters (around line 47-55), add:

```python
# Limit-order price buffer from mid (in basis points).
# Lower = closer to maker, may not fill. Higher = more likely to fill
# as taker, pays higher fee. Default 10 bps was chosen to sit inside
# most spreads on liquid pairs.
ENTRY_PRICE_BUFFER_BPS = 10  # 0.10% above mid for buys
EXIT_PRICE_BUFFER_BPS = 10   # 0.10% below mid for sells

def _limit_buy_price(price: float, pair: str) -> float:
    """Buy limit price = mid + ENTRY_PRICE_BUFFER_BPS bps."""
    buffered = price * (1 + ENTRY_PRICE_BUFFER_BPS / 10000)
    return round(buffered, _price_decimals(price, pair))


def _limit_sell_price(price: float, pair: str) -> float:
    """Sell limit price = mid - EXIT_PRICE_BUFFER_BPS bps."""
    buffered = price * (1 - EXIT_PRICE_BUFFER_BPS / 10000)
    return round(buffered, _price_decimals(price, pair))
```

### Step 2: Update sweep_dust (sell)

Find:
```python
limit_price = round(price * 0.998, _price_decimals(price, pair))
```

Replace with:
```python
limit_price = _limit_sell_price(price, pair)
```

### Step 3: Update Step 5a rotation

Find:
```python
limit_price = round(price * (1.002 if best_rot["side"] == "buy" else 0.998), _price_decimals(price, best_rot["pair"]))
```

Replace with:
```python
limit_price = (_limit_buy_price(price, best_rot["pair"])
               if best_rot["side"] == "buy"
               else _limit_sell_price(price, best_rot["pair"]))
```

### Step 4: Update Step 5b entry

Find:
```python
limit_price = round(best["price"] * 1.002, _price_decimals(best["price"], best["pair"]))
```

Replace with:
```python
limit_price = _limit_buy_price(best["price"], best["pair"])
```

### Step 5: Update Step 5c exit

Find:
```python
limit_price = round(ex["price"] * 0.998, _price_decimals(ex["price"], ex["pair"]))
```

Replace with:
```python
limit_price = _limit_sell_price(ex["price"], ex["pair"])
```

## Testing

1. **Syntax check:**
   ```bash
   python -c "import ast; ast.parse(open('scripts/cc_brain.py').read()); print('OK')"
   ```
2. **Helper unit test:**
   ```python
   from scripts.cc_brain import _limit_buy_price, _limit_sell_price
   # Buy at 1% buffer (100 bps) from $100 mid = $100.10
   # With default 10 bps = $100.01
   p = _limit_buy_price(100.0, "BTC/USD")
   print(f"Buy @ $100 mid: {p}")  # Expect ~100.0 (BTC pair_decimals = 1)
   p = _limit_sell_price(100.0, "BTC/USD")
   print(f"Sell @ $100 mid: {p}")  # Expect ~100.0
   ```
3. **Dry-run cycle** — look at the `ENTRY from USD: X/USD` log block's
   `WOULD: buy ... @ PRICE` line. The price should be within ~0.1% of
   the current analysis price, not 0.2%.

## Rollback

`git revert` the commit. The helpers are new, the call sites are
self-contained replacements. No data migration needed.

## Commit message

```
Tighten limit-price buffer from 0.2% to 0.1% (maker target)

Previous limit prices at mid * 1.002 (buy) and mid * 0.998 (sell)
aggressively crossed the spread on any liquid pair, paying Kraken's
0.25% taker fee per side. Observed 7-day average was 0.40% roundtrip
vs the 0.32% maker-rate ceiling.

New ENTRY_PRICE_BUFFER_BPS and EXIT_PRICE_BUFFER_BPS constants (both
10 bps = 0.10% default) set a tighter offset. This makes orders more
likely to rest on the book as maker fills. Unfilled orders get
cancelled after STALE_ORDER_HOURS by the existing check_pending_orders
path, so no new stuck-order risk is introduced.

Target: average fee roundtrip below 0.36%. If fill rate drops too
low, next step is to add a Kraken postOnly flag via the adapter layer.
```
