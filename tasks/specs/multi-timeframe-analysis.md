# Spec: Multi-Timeframe Analysis — 4H Direction + 15M Entry Confirmation (Phase 8)

**Date**: 2026-04-10
**Priority**: 2
**Status**: Shipped

## Motivation

The bot operates entirely on 1H bars. The scalper MTF hierarchy (4H direction, 1H structure, 15M setup, 5M entry) filters counter-trend entries and improves entry timing. Without a higher-timeframe filter, the bot enters bullish children during bearish 4H trends.

## Design

**4H gate adjusts confidence, not direction.** Counter-trend entries get 0.3x penalty, aligned get 1.15x boost, neutral passes through at 1.0x. The existing `min_confidence` threshold naturally filters counter-trend entries.

**15M gate defers entry, not cancels.** If 15M opposes, keep PLANNED and retry next cycle. After 6 consecutive failures, cancel. Avoids discarding setups the 4H/1H pipeline already approved.

Both gates start disabled via feature flags.

## Changes

### Phase 8A: 4H trend gate

- `core/config.py`: `MTF_4H_GATE_ENABLED`, `MTF_ALIGNED_BOOST` (1.15), `MTF_COUNTER_PENALTY` (0.3)
- `trading/pair_scanner.py:_scan_rotation_pair()`: After 1H belief, fetch 4H bars, run same TA ensemble, multiply confidence by alignment factor

### Phase 8B: 15M momentum confirmation

- `core/config.py`: `MTF_15M_CONFIRM_ENABLED`, `MTF_15M_MAX_DEFERRALS` (6)
- `runtime_loop.py:_execute_rotation_entries()`: Before order placement, check 15M belief alignment. Defer if opposing, cancel after max deferrals.

## Deployment

```env
MTF_4H_GATE_ENABLED=true
MTF_ALIGNED_BOOST=1.15
MTF_COUNTER_PENALTY=0.3
MTF_15M_CONFIRM_ENABLED=true
MTF_15M_MAX_DEFERRALS=6
```

## Tests

9 new tests: 5 for 4H gate (aligned boost, counter penalty, neutral passthrough, fetch failure, gate disabled), 4 for 15M gate (opposing defers, aligned proceeds, max deferrals cancels, gate disabled).
