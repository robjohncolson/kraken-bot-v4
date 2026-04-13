# Spec 29 -- Balances vs rotation-tree value drift

## Problem

The two portfolio value endpoints disagree by a large margin:

- `GET /api/balances` returns `{"cash_usd":"256.3707","total_value_usd":"256.756536883795655"}`
- `GET /api/rotation-tree` returns `total_portfolio_value_usd="426.11"` with 13 open roots (ADA, COMP, GBP, HYPE, LTC, MON, PEPE, SOL, SUI, TRU, USD, XMR, XRP).

`/api/balances` reads `portfolio.total_value_usd` at `web/routes.py:659`, which is the reconciled `Portfolio` object computed from live Kraken balances in `trading/portfolio.py`. `/api/rotation-tree` reads `tree_value_usd` built in `runtime_loop.py:3327-3328` by walking `rotation_tree` nodes and valuing each at `current_prices`.

Actual wallet state per balances is ~$256 USD, almost all of it fiat cash (the delta between `cash_usd` $256.37 and `total_value_usd` $256.76 is $0.39 -- essentially dust). But the rotation tree still lists roots for 12 crypto assets and values them at $426, a phantom $170 of position that the live wallet does not hold. Inspecting the tree, most of these roots have `realized_pnl=null`, `entry_price=null`, `fill_price=null`, `order_side=null`, `opened_at=null` -- they are bare `{asset, quantity_total}` rows with no provenance, strongly suggesting they were created when those assets briefly appeared in the reconciled portfolio and were never pruned when the assets left it.

This drift is user-visible on the dashboard and TUI, and more importantly it corrupts any layer-2 decision that reads `total_portfolio_value_usd` from `/api/rotation-tree` (cc_brain.py:1459 does exactly this and logs it as the tree value in brain reports). If the bot believes it holds $426 when it actually holds $256, position-sizing and self-tune rules operate on false premises.

## Desired outcome

1. The rotation tree no longer reports a total that exceeds the reconciled wallet's total value.
2. Orphan root nodes (roots whose `asset` has zero or effectively-zero balance in the reconciled portfolio) are either pruned or excluded from the value total, with the choice documented in the result file.
3. A diagnostic log line explains the discrepancy on every cycle where it would otherwise appear, so the orchestrator can observe it going forward.
4. `/api/balances.total_value_usd` and `/api/rotation-tree.total_portfolio_value_usd` agree to within a documented tolerance (e.g. $1 or 0.5%, whichever is larger) when the bot is in a steady state.

## Acceptance criteria

1. **Root cause diagnosis** written at the top of `tasks/specs/29-balances-tree-drift.result.md`:
   - Why are orphan roots being created? (Is it `bind_tree_to_position`, reconciliation, a CC API order path, or something else?)
   - Why are they never pruned? (Is there a pruner, and if so why didn't it fire?)
   - Confirm the hypothesis against the live `data/bot.db` by listing the 12 current orphan roots with their creation timestamps and last-update timestamps.

2. **Prune or exclude orphan roots from the value total** in `runtime_loop.py` where `tree_value_usd` is computed (around line 3327). Two acceptable approaches:
   - **(a) Prune**: walk `rotation_tree` roots, and for any root where the asset's reconciled balance is less than a small epsilon (e.g. the pair's `lot_decimals` minimum or $1 USD equivalent), mark the root `closed` and emit a `cc_memory` entry with `category='orphan_root_pruned'`, `pair=f"{asset}/USD"`, `content={quantity_total, reason: "no matching wallet balance"}`, `importance=0.5`.
   - **(b) Exclude**: leave the node in place (in case it's a transient reconciliation miss) but exclude it from `tree_value_usd` and add a new field `orphan_root_value_usd` that sums the excluded nodes separately. Emit the same `cc_memory` row the first time a root is flagged orphan.
   Pick one -- (a) is simpler and preferred unless there's a reason not to prune.

3. **Reconciliation tolerance check**: at the end of `tree_value_usd` computation, compare `tree_value_usd` to `portfolio.total_value_usd` (if the reconciled portfolio is available in this scope). If the absolute difference exceeds `max(Decimal("1.00"), total_value_usd * Decimal("0.005"))`, log a warning `rotation_tree_drift` with both values and the list of roots that contributed to the delta. Write a `cc_memory` row with `category='rotation_tree_drift'`, `importance=0.7` so the orchestrator can pick it up.

4. **Root pruning is idempotent and reversible**: if the pruned asset reappears in the reconciled portfolio on a later cycle (e.g. a manual buy), the existing tree-binding logic must be able to create a fresh root. Verify this by reading `_maybe_bind_tree_to_position` and documenting in the result file whether it already handles this case; do not add new binding logic unless it's broken.

5. **Tests**:
   - `tests/test_runtime_loop.py::test_tree_value_excludes_orphan_roots`: construct a rotation tree with two roots, one matching a portfolio balance and one not; assert `tree_value_usd` only counts the matching root.
   - `tests/test_runtime_loop.py::test_orphan_root_prune_writes_memory` (or `test_orphan_root_exclude_writes_memory` depending on choice): assert a `cc_memory` row with the correct category is written on first detection and NOT on subsequent detections for the same root (no spam).
   - `tests/test_runtime_loop.py::test_rotation_tree_drift_warning_threshold`: when `tree_value_usd` and `portfolio.total_value_usd` differ by less than the tolerance, no warning is emitted; when they differ by more, the warning and memory row are written.

6. Full pytest green.

## Non-goals

- Do not redesign the rotation tree schema.
- Do not touch `/api/balances` -- it's the ground truth, not the drift source.
- Do not retroactively fix or delete the current 12 orphan roots from the live database. The pruner will do this naturally on the next cycle after this spec lands; a one-shot cleanup script is out of scope.
- Do not change how `_maybe_bind_tree_to_position` creates roots; the bug is in pruning, not creation.
- Do not change `cc_brain.py:1459`'s logging. Once the value is correct, the log line is correct.

## Files in scope

- `runtime_loop.py` (tree value computation + pruner/excluder)
- `persistence/sqlite.py` (if the prune needs a new helper to mark `status='closed'`)
- `persistence/cc_memory.py` (only if a new helper is needed -- prefer the existing `write_memory` path)
- `tests/test_runtime_loop.py`
- `tasks/specs/29-balances-tree-drift.result.md`

## Out of scope (explicitly)

- `web/routes.py` -- no endpoint changes; the fix is in the tree value computation.
- `trading/portfolio.py` -- reconciled portfolio is the source of truth and doesn't need changes.
- `scripts/cc_brain.py` -- consumer of the fixed value, no changes needed.

## Evidence

- Live `/api/balances`: `{"cash_usd":"256.3707","total_value_usd":"256.756536883795655"}`
- Live `/api/rotation-tree`: 13 roots, `total_portfolio_value_usd="426.11"`, `open_count=2` (the actual open children are `root-usd-bdx-8` status=closing and `root-usd-crv-102` status=open)
- 11 of 13 roots have null `entry_price`/`fill_price`/`order_side`/`opened_at`, characteristic of reconciliation-created stubs that were never pruned.
- `data/bot.db` query: `SELECT asset, quantity_total, status FROM rotation_tree WHERE parent_node_id IS NULL ORDER BY asset;` confirms the orphan set.
