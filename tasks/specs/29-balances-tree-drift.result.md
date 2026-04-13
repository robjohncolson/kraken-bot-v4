## Diagnosis

- Root creation source: rotation roots are created by `RotationTreePlanner.initialize_roots()` in [runtime_loop.py](/abs/path/C:/Users/rober/Downloads/Projects/kraken-bot-v4/runtime_loop.py) startup initialization, which calls `build_root_nodes()` in `trading/rotation_tree.py`. Any reconciled balance above `min_position_usd` becomes a depth-0 `root-{asset}` node. Those roots start as bare balance-derived stubs with no execution provenance, which matches the orphan signature from the bug report.
- Why they persist: there was no runtime pass that reconciled existing roots back against `bot_state.balances` after startup. Once a root was seeded, it stayed in memory and in `rotation_nodes` until some other root-specific path happened to close it. That meant assets that left the wallet could remain as open roots indefinitely.
- Why the value drift inflated: `_compute_rotation_tree_value()` previously summed every depth-0 node without checking root status, so closed roots could still contribute if they remained in memory. It also had no orphan filter.
- Live DB confirmation: the current workspace DB does not match the older spec snapshot exactly. `data/bot.db` now has five open root stubs with no live children, all created at `2026-04-11 14:11:10`: `ASTER`, `AZTEC`, `BANANAS31`, `CRV`, and `GBP`. Each has `entry_price=NULL`, `fill_price=NULL`, `opened_at=NULL`, `closed_at=NULL`, and `live_children=0`. The only open root with live descendants is `USD` (`live_children=2`, children `root-usd-bdx-8` and `root-usd-crv-102`), so it is not an orphan candidate.
- Schema note: the spec asked for creation and last-update timestamps, but `rotation_nodes` has `created_at` and no `updated_at`. The closest lifecycle fields are `opened_at`, `deadline_at`, and `closed_at`, all of which are persisted and were used for the diagnosis above.

## `_maybe_bind_tree_to_position` finding

- The current `_maybe_bind_tree_to_position()` in [runtime_loop.py](/abs/path/C:/Users/rober/Downloads/Projects/kraken-bot-v4/runtime_loop.py) is for the conditional tree, not the rotation tree.
- It does not create or re-seed rotation roots.
- Fresh rotation roots are currently created only during runtime startup/reinitialization via `RotationTreePlanner.initialize_roots()`.
- Result: pruning an orphan root is safe for valuation correctness, but a later manual reappearance of that asset will not create a fresh rotation root during the same runtime session. It will only return after restart/reinitialization. Per the task, this was documented and no new binding logic was added here.

## Implementation

- Added a runtime-side orphan-root prune in the dashboard valuation path before `tree_value_usd` is computed.
- A root is pruned only when all of the following are true:
  - it is a live depth-0 node,
  - it has no live descendants,
  - its reconciled wallet balance is effectively zero by either `$1 USD` value or the pair `lot_decimals` minimum quantity when metadata exists.
- Pruned roots are marked `status='closed'`, stamped with `closed_at`, tagged with `exit_reason='orphan_root_pruned'`, persisted back through `save_rotation_tree()`, and recorded once via `cc_memory` category `orphan_root_pruned` with `importance=0.5`.
- Tree valuation now counts only live roots and returns per-root contribution details for diagnostics.
- Added a `rotation_tree_drift` warning and `cc_memory` write when `abs(tree_value_usd - portfolio.total_value_usd) > max($1, 0.5%)`. The payload includes both totals, the delta, the tolerance, the contributing live roots, and any roots pruned in that cycle.

## Tests Added

- `test_tree_value_excludes_orphan_roots`
- `test_orphan_root_prune_writes_memory`
- `test_rotation_tree_drift_warning_threshold`

## Verification

- No tests or lint were run in this subagent session because the subagent instructions explicitly prohibited verification commands after patching.
- GitNexus impact analysis was attempted before editing, but the GitNexus MCP calls were cancelled in this subagent context, so blast radius was checked manually from the local call sites instead.
