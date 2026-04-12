# Plan 03 — Fiat-currency filter

## Dependency

**Depends on spec 01** (floor-round exit qty). Both touch
`check_exits`. Run 01 first so the floor-round lands cleanly, then
run 03 on top.

## File targets

- `scripts/cc_brain.py` — only file to modify

## Step-by-step

### Step 1: Add FIAT_CURRENCIES constant

Near the existing `QUOTE_CURRENCIES` definition (around line 551,
right after `QUOTE_CURRENCIES = frozenset(...)`), add:

```python
# Non-USD fiat currencies. Held as leftover balances from conversions
# but not actively traded; excluded from exit scoring and rotation
# evaluation. Stability scoring still applies (currency-agnostic).
FIAT_CURRENCIES = frozenset((
    "EUR", "GBP", "AUD", "CAD", "CHF", "JPY", "ZAR", "HKD", "SGD",
))
```

### Step 2: Update check_exits

Find around line 876:
```python
if asset in QUOTE_CURRENCIES or h["value_usd"] < 5.0:
    continue
```

Change to:
```python
if asset in QUOTE_CURRENCIES or asset in FIAT_CURRENCIES or h["value_usd"] < 5.0:
    continue
```

### Step 3: Update evaluate_portfolio

Find around line 512:
```python
if from_asset in QUOTE_CURRENCIES:
    continue  # USD/USDT/USDC evaluated separately as "cash to deploy"
```

Change to:
```python
if from_asset in QUOTE_CURRENCIES or from_asset in FIAT_CURRENCIES:
    continue  # quote/fiat evaluated separately
```

Also, in the inner loop where `to_asset` is evaluated, find the skip:
```python
for to_asset, to_analysis in analysis_by_base.items():
    if to_asset == from_asset:
        continue
```

Add a fiat/quote skip for the target:
```python
for to_asset, to_analysis in analysis_by_base.items():
    if to_asset == from_asset:
        continue
    if to_asset in QUOTE_CURRENCIES or to_asset in FIAT_CURRENCIES:
        continue
```

### Step 4: Verify dust sweep is untouched

`find_dust_positions` (around line 900) already excludes USD/USDT/USDC
at line 907 but does NOT check FIAT_CURRENCIES. That's intentional for
this task — if the user wants to sweep small fiat balances as dust,
the existing path will find them. Leave it alone.

## Testing

1. **Syntax check:** `python -c "import ast; ast.parse(open('scripts/cc_brain.py').read())"`
2. **Dry-run:**
   ```bash
   python scripts/cc_brain.py --dry-run 2>&1 | grep -E "EXIT:|AUD|EUR|CAD|GBP"
   ```
   Must NOT show any `EXIT: AUD`, `EXIT: EUR`, etc. May show fiat
   in the holdings list (display is separate from exit scoring) but
   the exit section must not target them.
3. **Rotation proposals:** verify `evaluate_portfolio` output
   (dry-run log) no longer considers fiat as a rotation source or
   target.

## Rollback

`git revert` the commit. Clean, additive — the new constant is only
used by the two updated call sites.

## Commit message

```
Exclude non-USD fiat from exit scoring and rotation eval

Check_exits and evaluate_portfolio were treating AUD/EUR/CAD/GBP as
regular crypto positions. Their hold-scores would collapse on thin
fiat-fiat pairs, triggering exit proposals that failed or churned.

New FIAT_CURRENCIES set joins QUOTE_CURRENCIES in the skip list for
both exit scoring and rotation source/target consideration. Stability
scoring remains currency-agnostic; only the decision paths are
filtered.
```
