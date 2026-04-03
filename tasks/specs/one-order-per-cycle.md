# Spec: One Order Per Cycle

**Date**: 2026-04-03
**Priority**: 2
**Status**: Spec

## Motivation

Currently `_execute_rotation_entries()` iterates ALL PLANNED nodes and places orders for each one in a single cycle. This means up to N orders placed against a balance snapshot that goes stale after the first order. Pre-flight catches some cases, but the balance data is fundamentally stale after order #1.

Fix: place **one rotation entry per cycle**. Each order settles on Kraken between cycles (~30s), so the next pre-flight sees accurate balances.

## Design

### Change in `_execute_rotation_entries()`

Current flow:
```
for node in tree.nodes:
    if PLANNED and depth > 0:
        place_order(node)  # places N orders
```

New flow:
```
for node in tree.nodes:
    if PLANNED and depth > 0:
        place_order(node)
        return  # stop after first successful placement
```

That's it. One early return after the first successful order placement.

### Ordering priority

Nodes should be processed in a deterministic, useful order:
- Sort PLANNED nodes by `confidence` descending (best opportunities first)
- Tiebreak by `node_id` for determinism

## Affected Files

| File | Change |
|------|--------|
| `runtime_loop.py` | `_execute_rotation_entries()`: sort PLANNED nodes by confidence desc, return after first successful order |

## API / Function Contracts

No new functions. Single modification to existing loop in `_execute_rotation_entries()`.

## Edge Cases

1. **First node fails pre-flight**: Continue to next node (don't return on failure, only on success).
2. **All nodes fail**: Loop exhausts normally, no orders placed — same as today.
3. **Cancelled nodes during iteration**: Already handled by cooldown check — no change needed.
4. **Root exit orders**: These are placed by `_close_rotation_node()`, NOT by `_execute_rotation_entries()`. Root exits are unaffected by this change and should still place immediately.

## Test Plan

1. Unit: Only one order placed per call to `_execute_rotation_entries()`
2. Unit: Highest-confidence PLANNED node is selected first
3. Unit: If first node fails pre-flight, second node gets tried
4. Unit: Exit orders (TP/SL/timeout) are unaffected
