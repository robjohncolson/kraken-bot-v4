# Anti-Churn Spec — Rotation Tree Order Reduction

## Problem

Excessive order churn in the rotation tree:
- Too many PLANNED children per parent per scan cycle
- Capital spread too thin → entries fail at exchange (ordermin / insufficient funds)
- Failed entries → 30min pair cooldown → planner tries different pairs → same failure
- 30min entry timeout too short relative to 2-48h estimated windows

## Root Causes

1. **No candidate cap**: `compute_child_allocations()` can produce 10+ children per parent if scanner returns many candidates. With $200 parent and 15 candidates, each child gets ~$10-13 — barely above `min_position_usd` and often below pair-specific ordermin.

2. **Replanning after partial fills**: When child-1 fills (PLANNED→OPEN) and parent still has free capital, parent becomes a leaf again. Next plan_cycle creates child-2, child-3, etc. This is INTENTIONAL but amplifies churn when combined with #1.

3. **Entry timeout too short**: 30-minute timeout on a 12-hour window means only 4% of window before cancel. Market moves slowly for limit orders.

4. **No per-parent child limit**: A root node with $200 could spawn 10 children across 2 scan cycles, each with $10-20 allocated.

## Solution

### Fix 1: Cap candidates per parent (CRITICAL)

In `compute_child_allocations()`, add `max_children: int = 3` parameter. After scoring and sorting by weight, take only the top N.

**Why 3**: With PARENT_DEPLOY_RATIO=0.80, 3 children at ~27% each is a reasonable concentration. 5+ children dilute too much.

### Fix 2: Cap total live children per parent (CRITICAL)

In `plan_cycle()`, before scanning, count existing live children of the leaf. If `len(existing_children) >= max_children_per_parent`, skip scanning for that leaf.

```python
MAX_CHILDREN_PER_PARENT = 3

# In plan_cycle, for each leaf:
existing_children = [n for n in live_nodes(updated_tree) if n.parent_node_id == leaf.node_id]
if len(existing_children) >= MAX_CHILDREN_PER_PARENT:
    continue
remaining_slots = MAX_CHILDREN_PER_PARENT - len(existing_children)
```

Then pass `remaining_slots` as `max_children` to `compute_child_allocations()`.

### Fix 3: Dynamic entry timeout (HIGH)

Instead of flat 30 minutes, use `min(window_hours * 0.25, 120) * 60` (25% of estimated window, capped at 2 hours).

Store on the PendingOrder or derive from the node's `window_hours` at timeout check time.

**Simpler approach**: Use the node's `window_hours` field (already set at planning time). In `_check_rotation_fill_timeouts()`:
```python
# Replace flat timeout:
node_timeout = timedelta(minutes=min(
    (node.window_hours or 1) * 60 * 0.25,  # 25% of window in minutes
    120,  # cap at 2 hours
))
if age >= node_timeout:
    ...
```

### Fix 4: Increase min allocation per child (MEDIUM)

The current `min_position` in `compute_child_allocations()` is `$10` (from config). This is too low — most Kraken pairs need $15-50 notional minimum. 

Change the effective minimum to `max(min_position_usd, 20)` to ensure each child has enough capital. Better: compute the minimum from ordermin × reference_price for each candidate (already done in the planner ordermin filter, but that runs AFTER allocation).

**Better**: Pre-filter candidates in the planner BEFORE passing to `compute_child_allocations()`. Remove candidates whose ordermin × reference_price > parent free / max_children. This way allocation never creates undersized children.

### Fix 5: Add MAX_CHILDREN_PER_PARENT to config (LOW)

New env var `ROTATION_MAX_CHILDREN_PER_PARENT` with default 3.

## Affected Files

- `core/config.py` — add `ROTATION_MAX_CHILDREN_PER_PARENT` default + Settings field
- `trading/rotation_tree.py` — add `max_children` param to `compute_child_allocations()`
- `trading/rotation_planner.py` — enforce child cap per parent, pre-filter by ordermin notional
- `runtime_loop.py` — dynamic entry timeout in `_check_rotation_fill_timeouts()`

## Test Plan

- Unit: verify `compute_child_allocations()` returns at most `max_children` results
- Unit: verify planner skips leaves with >= max_children live children
- Unit: verify dynamic timeout uses window_hours
- Unit: verify pre-filter by ordermin notional removes undersized candidates
- Regression: all 560 existing tests pass

## Success Criteria

- PLANNED→CANCELLED ratio drops from ~50%+ to <20%
- Maximum 3 children per parent per cycle
- Entry timeout proportional to estimated window
- No "insufficient funds" errors from thin capital spread
