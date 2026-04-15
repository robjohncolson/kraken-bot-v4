# Result 35 -- Untracked-asset wallet sweep

## Status: READY TO COMMIT

Sonnet implemented (2 passes), Codex independently reviewed, Sonnet
addressed the medium finding in a third pass. CC verified.

## What shipped

- `scripts/cc_brain.py`
  - New constants `_FIAT_ASSETS`, `_WALLET_DUST_MIN_USD`
  - New `find_wallet_only_untracked(holdings, open_root_assets, min_value_usd, recently_stuck)`
    -- returns sweep-compatible dicts for crypto balances not in any open
    rotation root, excluding fiat, stablecoins, tracked roots, sub-$0.50
    dust, and assets already in the 48h stuck_dust cooldown
  - `run_brain()` wallet sweep block: queries
    `/api/memory?category=stuck_dust&hours=48`, builds suppression set
    from each row's `pair` field (primary) and `content.asset` (fallback),
    then calls `find_wallet_only_untracked` and reuses `sweep_dust()`
  - **Pre-existing bug fix**: `sweep_dust()` failure path was writing
    `category="observation"` with `content.type="stuck_dust"`, but the
    orchestrator (`scripts/dev_loop.ps1:535` and the prompt files) queries
    `category='stuck_dust'` directly. Fixed to write `category="stuck_dust"`,
    `pair=d["asset"]`, and dropped the redundant `content.type` field.
- `scripts/dev_loop_prompt.md`
  - Added 3-bullet clarification under priority-5 reconciliation rule:
    non-fiat untracked symbols are NOT benign and should trigger a sweep
    even below the count threshold
- `tests/test_cc_brain_untracked_sweep.py` -- 11 tests (all green)
  - Wallet-only sweep filtering: fiat / stablecoin / tracked / threshold / qty
  - stuck_dust suppression: respects `recently_stuck`, default unchanged,
    end-to-end parsing of `/api/memory` response shape
  - Volume-min failure path writes `stuck_dust` category correctly
- `tasks/specs/35-untracked-asset-sweep.{spec,plan}.md` -- design docs

## Codex review findings (and resolutions)

| # | Severity | Finding | Resolution |
|---|----------|---------|------------|
| 1 | Medium | `stuck_dust` was written but never *read* before retrying — perpetual-fail tokens would be re-attempted every brain cycle | Added 48h cooldown query in `run_brain()`; `find_wallet_only_untracked` now skips assets in the cooldown set. New tests cover the suppression path |
| 2 | Low | Tests cover the helper + `sweep_dust()` directly but not the `run_brain()` integration boundary | New `test_stuck_dust_set_built_from_memory_response` exercises the dual-field parsing logic end-to-end |

Codex confirmed core helper logic is correct (fiat/stablecoin/tracked-root
exclusion, `$0.50` cutoff, `qty>0` filter), and that the stuck_dust category
fix matches what `scripts/dev_loop.ps1:535` queries.

## Verification

| Step | Result |
|------|--------|
| `pytest tests/test_cc_brain_untracked_sweep.py -x -q` | 11 passed |
| `pytest tests/ -x -q` | 736 passed, 1 skipped (725 baseline + 11 new) |
| Codex independent review | 2 findings, both addressed |
| `find_wallet_only_untracked` lints | n/a -- no lint suite |

## Residual risk

Any intentionally-held manual non-fiat wallet balance not represented in a
rotation root will be auto-sold by design. Mitigation: 48h stuck_dust
cooldown will prevent a perpetual loop on a real failure, and the user can
manually create a rotation node for any balance they want to keep.

## Follow-ups (out of scope)

1. Apply the same stuck_dust suppression to the original
   `find_dust_positions()` path in `sweep_dust`. Pre-existing behavior,
   not introduced by this spec.
2. Forward-data check: after the next live brain cycle, confirm
   `cc_memory.category='stuck_dust'` actually populates with the new format
   and the suppression set is honored.
