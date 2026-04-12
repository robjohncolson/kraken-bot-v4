# Plan 10 — Fix the backwards self-tune rule

## File targets

- `scripts/cc_brain.py` — only file to modify

## Step-by-step

### Step 1: Locate the rule

Around line 711 in `scripts/cc_brain.py`:

```python
# Rule 3: Fee burden too high — increase position size
if gross_wins > 0 and total_fees / gross_wins > 0.60 and MAX_POSITION_PCT < _POSITION_PCT_MAX:
    old = MAX_POSITION_PCT
    MAX_POSITION_PCT = round(MAX_POSITION_PCT + 0.01, 2)
    fee_pct = total_fees / gross_wins
    log_fn(f"  TUNE: MAX_POSITION_PCT {old} -> {MAX_POSITION_PCT} (fees={fee_pct:.0%} of wins)")
    _record_param_change("MAX_POSITION_PCT", old, MAX_POSITION_PCT,
                          f"fees {fee_pct:.0%} of gross wins")
    return
```

### Step 2: Replace with a correct rule

Delete the above block. Insert a new rule that raises `ENTRY_THRESHOLD`
when fees dominate wins — the logic being "if fees are eating your
wins, be more selective about which setups you take":

```python
# Rule 3: Fee burden too high — tighten entry threshold so only
# high-conviction setups pay the fee tax. Position size doesn't help
# here: fee ratio is invariant to position size at fixed percentage fees.
if gross_wins > 0 and total_fees / gross_wins > 0.60 and ENTRY_THRESHOLD < _ENTRY_THRESHOLD_MAX:
    old = ENTRY_THRESHOLD
    ENTRY_THRESHOLD = round(ENTRY_THRESHOLD + 0.05, 2)
    fee_pct = total_fees / gross_wins
    log_fn(f"  TUNE: ENTRY_THRESHOLD {old} -> {ENTRY_THRESHOLD} (fees={fee_pct:.0%} of wins — be pickier)")
    _record_param_change("ENTRY_THRESHOLD", old, ENTRY_THRESHOLD,
                          f"fees {fee_pct:.0%} of gross wins, tighten entry")
    return
```

### Step 3: Note that this interacts with Rule 1

Rule 1 already raises `ENTRY_THRESHOLD` when win rate is below 30%.
Rule 3 (new) also raises it when fees dominate wins. They don't
double-fire in a single cycle (the `return` at the end of each rule
prevents it), but over many cycles they could stack and push
`ENTRY_THRESHOLD` to `_ENTRY_THRESHOLD_MAX` (0.85) quickly.

That's OK for now — the cap at 0.85 prevents runaway. If observed
behavior shows the rules fighting each other, consolidate them in a
follow-up spec.

### Step 4: Optional — leave an explanatory comment

Right above the rule block, keep or add:

```python
# NOTE: Rule 3 previously bumped MAX_POSITION_PCT when fees dominated
# wins. That was mathematically wrong — fee ratio is invariant to
# position size at fixed percentage fees. Rule now bumps
# ENTRY_THRESHOLD so the bot takes fewer but higher-conviction trades.
```

## Testing

1. **Syntax check:** `python -c "import ast; ast.parse(open('scripts/cc_brain.py').read())"`
2. **Sanity test** (inline or in tests/ if helpful):
   ```python
   # Synthetic outcomes — 50% fee ratio, win rate 50%
   outcomes = [
       {"net_pnl": 1.0, "fee_total": 0.5, "exit_reason": "timer"},
       {"net_pnl": 1.0, "fee_total": 0.5, "exit_reason": "timer"},
       {"net_pnl": -1.0, "fee_total": 0.5, "exit_reason": "stop_loss"},
       {"net_pnl": -1.0, "fee_total": 0.5, "exit_reason": "stop_loss"},
       {"net_pnl": 1.0, "fee_total": 0.5, "exit_reason": "timer"},
   ]
   from scripts import cc_brain
   prev_thr = cc_brain.ENTRY_THRESHOLD
   prev_pos = cc_brain.MAX_POSITION_PCT
   cc_brain.self_tune(outcomes, [], print)
   # ENTRY_THRESHOLD should have bumped (fees=total_fees/gross_wins = 2.5/3.0 = 83%)
   # MAX_POSITION_PCT should be UNCHANGED
   assert cc_brain.MAX_POSITION_PCT == prev_pos, "position size changed!"
   ```

## Rollback

`git revert` the commit. Restores the original (backwards) rule.

## Commit message

```
Fix backwards self-tune rule for fee burden

self_tune's Rule 3 was bumping MAX_POSITION_PCT when fees consumed
>60% of gross wins. That's mathematically wrong: at fixed percentage
fees, the fees/gross_wins ratio is INVARIANT to position size. The
rule couldn't converge and pushed MAX_POSITION_PCT from 0.04 to 0.07
over recent cycles without fixing anything.

New rule bumps ENTRY_THRESHOLD instead: if fees dominate wins, be
pickier about which setups to take. That's a lever that actually
changes the fee/win ratio — only trades above a higher threshold
incur the fee tax, so the net P&L per committed dollar improves.

MAX_POSITION_PCT is left at whatever value the previous buggy rule
pushed it to. That's a separate tuning concern.
```
