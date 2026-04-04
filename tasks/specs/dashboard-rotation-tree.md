# Dashboard Rotation Tree + Expired Root Recovery

**Date**: 2026-04-04
**Status**: Implemented

## Motivation

The rotation tree API endpoint (`/api/rotation-tree`) and SSE event stream (`rotation_events`) exist in the backend but have no frontend rendering. The dashboard shows portfolio, positions, beliefs, stats, and reconciliation — but not the core rotation tree that drives all trading. Operators viewing the dashboard on LAN have no visibility into tree state, TA direction, deadlines, or rotation events.

Additionally, two bugs were discovered during observation:
1. **P&L disappears** when root nodes enter CLOSING or EXPIRED status — snapshot builder only computed unrealized P&L for OPEN roots
2. **EXPIRED roots are permanently abandoned** — when `_close_rotation_node` can't place an exit order (no WebSocket price, no fallback), the root gets stuck in EXPIRED forever with no recovery path

## Changes

### 1. TA Direction Persistence (`ta_direction` field)

**Problem**: `evaluate_root_ta()` returns `(direction, window_hours, confidence)` but direction is used for logic then discarded. Not stored on the node, not exposed in API.

**Fix**: Add `ta_direction: str | None` to `RotationNode`, persist in SQLite, expose in `RotationNodeSnapshot`.

**Affected files**:
- `core/types.py` — new field on `RotationNode`
- `persistence/sqlite.py` — migration, save, fetch
- `runtime_loop.py` — store direction in `_evaluate_root_deadlines` and `_handle_root_expiry`
- `web/routes.py` — new field on `RotationNodeSnapshot`
- `runtime_loop.py` (`_build_rotation_tree_snapshot`) — include in snapshot

### 2. Dashboard Rotation Tree Panel

**Problem**: No frontend rendering of tree state.

**Fix**: Full-width panel with summary bar (tree value, open/closed counts, deployed, realized P&L) and tree table (Asset, Status, Direction, Confidence, Deadline, TTL, P&L). Nodes displayed in DFS order with depth-based indentation. Cancelled nodes filtered out.

**Affected files**:
- `web/static/index.html` — new panel section
- `web/static/app.js` — `updateRotationTree` handler, initial fetch from `/api/rotation-tree`
- `web/static/styles.css` — direction/status badges, TTL color classes

**Visual elements**:
- Direction badges: green=bullish, red=bearish, gray=neutral
- Status badges: green=open, blue=planned, yellow=closing, gray=closed
- TTL: green >2h, yellow <2h, red <30min, bold red EXPIRED
- P&L: green positive, red negative

### 3. Dashboard Rotation Events Panel

**Problem**: SSE already sends `rotation_events` but frontend ignores them.

**Fix**: Full-width panel with chronological event feed (most-recent-first). Color-coded event type badges.

**Affected files**:
- `web/static/index.html` — new panel section
- `web/static/app.js` — `updateRotationEvents` handler

**Event type colors**: fill_entry=blue, fill_exit=purple, tp_hit=green, sl_hit=red, entry_timeout=amber, exit_escalation=amber, root_extended=green, root_exit=red

### 4. P&L for CLOSING/EXPIRED Roots (Bug Fix)

**Problem**: `_build_rotation_tree_snapshot` only computes unrealized P&L for `status == OPEN` roots. CLOSING and EXPIRED roots still hold assets but show no P&L.

**Fix**: Extend condition to include `CLOSING` and `EXPIRED` statuses.

**Affected file**: `runtime_loop.py` line ~2054

### 5. Expired Root Recovery (Bug Fix)

**Problem**: When `_close_rotation_node` fails to place an exit order (no price available), it marks the root as EXPIRED — a terminal state with no recovery. The asset is permanently abandoned in the tree.

**Root cause**: For roots, `entry_price` is None (never entered via an order). If WebSocket doesn't have the pair subscribed, `_close_rotation_node` has no price fallback.

**Fix** (two parts):
1. **Price fallback**: `_handle_root_expiry` now stores the OHLCV close price (from bars it already fetched for TA) as `entry_price` on the node before calling `_close_rotation_node`
2. **Recovery loop**: `_evaluate_root_deadlines` now detects EXPIRED roots and resets them to OPEN with `deadline_at=None`, so they re-enter TA evaluation on the next cycle

**Affected file**: `runtime_loop.py` — `_handle_root_expiry` (price fallback), `_evaluate_root_deadlines` (recovery)

## Edge Cases

- **Infinite retry loop**: Mitigated by `recovery_count` field on RotationNode (max 3 recoveries). After 3 EXPIRED→OPEN cycles, the root stays permanently EXPIRED. Counter persisted in SQLite, survives restart
- **Stale OHLCV price**: The close price used as fallback could be up to 5 minutes old (OHLCV cache TTL). For a limit sell order, this is acceptable — worst case the order sits unfilled and gets re-evaluated
- **Dashboard with no SSE**: Initial fetch from `/api/rotation-tree` on page load ensures the tree renders even before the first SSE update

## Test Plan

- All 607 existing tests pass (no regressions)
- Manual verification:
  - `curl /api/rotation-tree` returns `ta_direction` field on root nodes
  - Dashboard renders rotation tree panel with direction badges
  - Rotation events panel populates from SSE stream
  - CLOSING/EXPIRED roots show unrealized P&L
  - EXPIRED roots recover to OPEN on next cycle
