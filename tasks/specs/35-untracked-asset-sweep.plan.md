# Plan 35 -- Untracked-asset wallet sweep

## Verified root cause

`find_dust_positions()` in `scripts/cc_brain.py` iterates over `open_positions`
(rotation tree roots). FLOW and TRIA are in Kraken wallet but have no rotation
node, so they are never passed to `sweep_dust()`. The reconciler sees them every
cycle and fires `reconciliation_anomaly` memories indefinitely.

The orchestrator (`dev_loop_prompt.md`) Step 2 priority-5 says "Reconciliation
discrepancy logged >= 3 times in 24h" but provides no guidance distinguishing
fiat-held vs crypto-held untracked symbols, leading to wrong deferral.

## Implementation steps

### Step 1: Add `_FIAT_ASSETS` constant (scripts/cc_brain.py)

After the existing `_SKIP_BASES` definition (line ~144), add:

```python
# Fiat currencies: never sell these as "untracked dust"
_FIAT_ASSETS = frozenset(("USD", "GBP", "EUR", "AUD", "CAD", "CHF", "JPY"))
```

`_SKIP_BASES` already contains GBP/EUR/AUD/CAD/CHF/JPY (they're in the fiat
filter), but NOT USD. We need both `_FIAT_ASSETS` (for the new function) and
`_SKIP_BASES` (for pair discovery filtering) to remain separate — don't merge.

### Step 2: Add `find_wallet_only_untracked()` (scripts/cc_brain.py)

Insert after `find_dust_positions()` (after line ~1325), before `sweep_dust()`:

```python
_WALLET_DUST_MIN_USD = 0.50  # Don't bother selling below this

def find_wallet_only_untracked(
    holdings: list[dict],
    open_root_assets: set[str],
    min_value_usd: float = _WALLET_DUST_MIN_USD,
) -> list[dict]:
    """Find wallet balances not in any open rotation node.

    Returns sweep-compatible dicts: {asset, qty, usd_value}.
    Skips fiat (USD/GBP/EUR/...), stablecoins (USDT/USDC/DAI/PYUSD),
    assets already in an open rotation root, and balances < min_value_usd.
    """
    results = []
    for h in holdings:
        asset = h["asset"]
        if asset in _FIAT_ASSETS:
            continue
        if asset in _SKIP_BASES:
            continue
        if asset in open_root_assets:
            continue
        usd_val = float(h.get("value_usd", 0))
        if usd_val < min_value_usd:
            continue
        qty = float(h.get("qty", 0))
        if qty <= 0:
            continue
        results.append({"asset": asset, "qty": qty, "usd_value": usd_val})
    return results
```

### Step 3: Wire into `run_brain()` (scripts/cc_brain.py)

After the existing dust sweep block (~line 1775), add:

```python
    # Wallet-only untracked sweep — sell crypto balances not in any rotation node
    open_root_assets = {p["asset"] for p in open_positions}
    wallet_untracked = find_wallet_only_untracked(holdings, open_root_assets)
    if wallet_untracked:
        log(f"\n  Wallet untracked sweep: {len(wallet_untracked)} asset(s) not in rotation tree")
        sweep_dust(wallet_untracked, dry_run, log)
    else:
        log("  No wallet-only untracked assets to sweep.")
```

Key: `open_positions` is already in scope at this point in `run_brain()`.
`holdings` is already in scope from Step 2 (Observe).

### Step 4: Edit `dev_loop_prompt.md`

Find priority-5 in Step 2 -- Diagnose:

```
5. **Reconciliation discrepancy** logged >= 3 times in 24h -- state-machine drift
```

Replace with:

```
5. **Reconciliation discrepancy** logged >= 3 times in 24h -- state-machine drift
   - If `untracked_asset_symbols` contains any non-fiat token (anything other than
     USD/GBP/EUR/AUD/CAD/CHF/JPY), this is NOT benign -- it is real wallet drift
     the bot cannot see. Treat as high-priority signal regardless of count.
   - USD/GBP/EUR/AUD/CAD/CHF/JPY untracked symbols = potentially benign fiat hold.
   - Non-fiat untracked symbols (crypto, stablecoins) = dispatch a sweep spec
     even if the anomaly count is below 3.
```

### Step 5: Write `tests/test_cc_brain_untracked_sweep.py`

Eight tests per spec acceptance criteria. Use `monkeypatch` on `cc_brain.fetch`
for the sweep-path tests (g, h). Call functions directly for unit tests (a-f).
No `__init__.py` needed — this goes in the top-level `tests/` directory.

### Step 6: Fix `stuck_dust` memory category (both write sites in `sweep_dust()`)

Pre-existing bug: `sweep_dust()` was writing `category='observation'` with
`content.type='stuck_dust'`, but the orchestrator settled-check queries
`category='stuck_dust'`. Fix both the original write site (line ~1394) and any
new write site added by spec 35 to emit:

```python
{
    "category": "stuck_dust",
    "pair": d["asset"],
    "content": {"asset": d["asset"], "qty": d["qty"], "reason": result["error"]},
    "importance": 0.3,
}
```

Drop the `content.type` field (it's now redundant with `category`). Update the
corresponding test assertion from `mem["content"]["type"] == "stuck_dust"` to
`mem["category"] == "stuck_dust"` and `mem["pair"] == asset`.

### Step 7: Run pytest

```
C:/Python313/python.exe -m pytest tests/test_cc_brain_untracked_sweep.py -x -q
C:/Python313/python.exe -m pytest tests/ -x -q
```

Expect: 8 new tests + 725 baseline = 733 passed, 1 skipped.

## Owned paths

- `scripts/cc_brain.py`
- `scripts/dev_loop_prompt.md`
- `tests/test_cc_brain_untracked_sweep.py` (new)
- `tasks/specs/35-untracked-asset-sweep.spec.md` (updated acceptance criteria)
- `tasks/specs/35-untracked-asset-sweep.plan.md` (this file)

## Owned paths

- `scripts/cc_brain.py`
- `scripts/dev_loop_prompt.md`
- `tests/test_cc_brain_untracked_sweep.py` (new)
