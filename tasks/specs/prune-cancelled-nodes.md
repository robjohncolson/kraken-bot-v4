# Spec: Prune Cancelled Nodes from TUI

**Date**: 2026-04-03
**Priority**: 4 (lowest)
**Status**: Spec

## Motivation

Cancelled nodes accumulate in the rotation tree forever. They clutter the TUI rotation tree view (screen 7) with red CANCELLED entries that provide no operational value.

## Design

### Option chosen: Filter in TUI widget (don't modify tree state)

Cancelled nodes should remain in the tree state for audit/history purposes (and are already persisted in SQLite). The TUI simply stops displaying them.

### Change in `tui/widgets/rotation_tree.py`

In `refresh_content()` (line 51+), the DFS traversal iterates all nodes. Add a filter:

```python
if node.status == "cancelled":
    continue  # skip cancelled nodes in display
```

This keeps cancelled nodes in:
- SQLite (for history)
- API endpoints (for debugging)
- Tree state (for consistency)

But hides them from the operator's TUI view where they're just noise.

### Optional: Add TUI toggle

A stretch goal would be a toggle key (e.g., `c`) to show/hide cancelled nodes. But for now, always hiding them is the right default.

## Affected Files

| File | Change |
|------|--------|
| `tui/widgets/rotation_tree.py` | Skip cancelled nodes in `refresh_content()` DFS traversal |

## Edge Cases

1. **All children cancelled**: Parent node still shows, just with no visible children. This is correct — it shows the parent is active but all rotations failed.
2. **Node count display**: If there's a node count shown, it should either count only visible nodes or be removed. Need to check.

## Test Plan

1. Unit: Cancelled nodes are excluded from TUI tree rendering
2. Unit: Non-cancelled nodes (OPEN, PLANNED, CLOSING, CLOSED, EXPIRED) still display
3. Unit: Parent with all-cancelled children still renders (as a leaf)
