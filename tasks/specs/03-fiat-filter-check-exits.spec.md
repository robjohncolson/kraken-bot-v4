# Spec 03 — Fiat-currency filter in check_exits

## Problem

The bot holds leftover fiat currency balances from previous conversions:
AUD ($8), CAD ($21), EUR ($5), GBP ($14). These are NOT active trading
positions — they're stuck fiat dust from historical conversions. But
`check_exits()` only excludes `USD`, `USDT`, and `USDC` (in the
`QUOTE_CURRENCIES` set), so it treats AUD/CAD/EUR/GBP/CHF/JPY as
regular crypto positions.

Every cycle, `check_exits` runs a hold-score check on AUD/USD,
EUR/USD, etc. These pairs are thin and volatile in fiat-fiat terms;
hold scores collapse below 0.20; the bot proposes an "exit" sell.
Those sells also fail (for separate reasons handled in specs 01 and
02), but even if they succeeded, the bot shouldn't be actively
trading tiny fiat dust on every cycle — it's log noise and wasted
effort.

## Desired outcome

Non-USD fiat currencies are excluded from the exit-scoring path the
same way USD/USDT/USDC already are. Fiat dust stays put; the bot
ignores it for hold-score purposes.

## Acceptance criteria

1. A new module-level constant `FIAT_CURRENCIES = frozenset((...))`
   lists non-USD fiats: EUR, GBP, AUD, CAD, CHF, JPY (and any
   others Kraken lists as fiat).
2. `check_exits` skips any asset in `QUOTE_CURRENCIES | FIAT_CURRENCIES`.
3. The rotation logic (`evaluate_portfolio`) also skips these
   assets as both sources and targets — fiat is neither a position
   to rotate away from nor a target to rotate into.
4. The dust-sweep logic is unchanged: if a fiat balance is truly
   tiny (below dust threshold), it may still be swept, but that's
   the dust path, not the exit-scoring path.
5. After the fix, a cycle log no longer contains an `EXIT: AUD`
   or `EXIT: EUR` line.

## Non-goals

- Do not delete existing fiat balances or auto-convert them.
- Do not add any fiat-specific pricing logic. Fiat is already priced
  correctly by `compute_portfolio_value` for display purposes.
- Do not modify `QUOTE_CURRENCIES` itself — leave it for the
  cash-deployment path. Add `FIAT_CURRENCIES` as a separate set.
- Do not touch the stability scoring for fiat (currency-agnostic
  stability is already correct).

## Evidence

`state/cc-reviews/brain_2026-04-12_*.md` — search for `EXIT: AUD`.
Multiple cycles show repeated failing AUD exit proposals.

The exchange-balances response shows:
```
AUD  avail=12.6357
CAD  avail=29.3043
EUR  avail=4.4491
GBP  avail=11.0749
```

All small amounts that should never have become exit targets.
