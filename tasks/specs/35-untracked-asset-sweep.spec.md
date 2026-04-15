# Spec 35 -- Untracked-asset wallet sweep

## Problem

Two assets (FLOW and TRIA) have lived in the Kraken wallet for weeks but are
not represented in any open rotation node. The reconciler correctly flags them
as `untracked_assets` every cycle, producing `reconciliation_anomaly` memories
forever. Spec 33 widened the dedupe window to suppress the noise, but the
underlying drift was never resolved.

Combined wallet exposure: ~$61 (FLOW ~$0.04 each + TRIA), vs bot-tracked
`total_value_usd=$306.96`. The $61.35 gap means the bot is flying blind on
roughly 17% of the portfolio.

Additionally, the orchestrator's `dev_loop_prompt.md` has been classifying
`reconciliation_anomaly` with non-fiat `untracked_asset_symbols` as "benign
held-fiat noise" — which is wrong when the symbols are crypto tokens.

## Root causes

### Root cause 1 — dust sweep misses wallet-only balances

`scripts/cc_brain.py:find_dust_positions()` scans `open_positions` (rotation
tree roots), not wallet balances. Assets that are in Kraken's wallet but not
in any rotation node are invisible to the sweep. The reconciler's
`recommended_action=AUTO_FIX / recommended_step=import_asset_balance` path
is not wired up for standalone untracked balances.

### Root cause 2 — orchestrator wrong deferral

`scripts/dev_loop_prompt.md` Step 2 priority-5 description does not
distinguish between untracked fiat (which can be benign) and untracked crypto
(which is always real drift). The orchestrator has been ignoring a legitimate
signal as noise.

## Desired outcome

1. Each brain cycle, any wallet balance that is non-fiat, non-stablecoin, not
   in any open rotation node, and worth > $0.50 is detected and sold via the
   same dust-sweep code path.
2. On `EOrder:Insufficient funds` or `volume minimum not met`, a `stuck_dust`
   memory entry is written so future cycles don't keep retrying.
3. The orchestrator is taught that `untracked_asset_symbols` containing
   non-fiat tokens is a high-priority signal, not noise.

## Acceptance criteria

### Part A — `scripts/cc_brain.py`

1. **New function `find_wallet_only_untracked()`** (or equivalent helper)
   that takes:
   - `holdings: list[dict]` — wallet holdings from `compute_portfolio_value()`
   - `open_root_assets: set[str]` — assets in open rotation nodes
   - `min_value_usd: float` — skip assets below this threshold (use $0.50)
   Returns a list of dicts compatible with `sweep_dust()`: each entry has
   `asset`, `qty`, `usd_value`.

2. **Skip rules** (all must hold to include an asset):
   - Asset not in `_FIAT_ASSETS` (`USD`, `GBP`, `EUR`, `AUD`, `CAD`, `CHF`,
     `JPY`) — these are fiat, not crypto
   - Asset not in `_SKIP_BASES` (existing constant: stablecoins + fiat)
   - Asset not already in `open_root_assets`
   - `usd_value >= min_value_usd` (default $0.50)

3. **In `run_brain()`**, after the existing dust sweep block (line ~1772),
   call `find_wallet_only_untracked()` and pass results to `sweep_dust()`.
   Log output must be distinct from the existing rotation-dust log.

4. **Do NOT touch** `runtime_loop.py`, `trading/rotation_tree.py`,
   `trading/reconciler.py`, or `exchange/`. Fix is contained to
   `scripts/cc_brain.py`.

5. **Do NOT create rotation roots** for untracked assets. This is sell-only.

### Part B — `scripts/dev_loop_prompt.md`

6. Under Step 2 -- Diagnose, priority-5 entry ("Reconciliation discrepancy"),
   add a clarifying note (1–3 bullets, matching existing style) that explains:
   - When `untracked_asset_symbols` contains any non-fiat token, it is NOT
     benign — it is real wallet drift.
   - USD/GBP/EUR/AUD/CAD/CHF/JPY untracked = potentially benign fiat hold.
   - Non-fiat untracked symbols (crypto, stablecoins) = treat as high-priority
     signal regardless of count.

### Part C — Tests

7. **New test file** `tests/test_cc_brain_untracked_sweep.py`:

   a. `test_wallet_only_untracked_includes_non_fiat_not_in_tree` — FLOW in
      holdings, not in open_root_assets, value=$0.60 → returned as sweep target.

   b. `test_wallet_only_untracked_skips_usd` — USD in holdings, value=$100 →
      not returned.

   c. `test_wallet_only_untracked_skips_fiat_gbp` — GBP in holdings, value=$10
      → not returned.

   d. `test_wallet_only_untracked_skips_stablecoin` — USDT in holdings,
      value=$5 → not returned.

   e. `test_wallet_only_untracked_skips_already_in_tree` — SOL in holdings and
      in open_root_assets → not returned.

   f. `test_wallet_only_untracked_skips_below_threshold` — FLOW in holdings,
      value=$0.04 → not returned (below $0.50).

   g. `test_wallet_only_untracked_volume_min_failure_writes_stuck_dust` — stub
      `fetch` to return `{"error": "EOrder:Volume minimum not met"}` for the
      sell order; assert one memory written with `category == "stuck_dust"`,
      `pair == asset`, and `content.asset == asset` (no `content.type` field).

   h. `test_wallet_only_untracked_sell_success` — stub `fetch` to return
      `{"txid": "ABC123"}` for the sell order; assert `action == "sold"` in
      result.

## Pre-existing bug fix (bonus, landed with spec 35)

A latent inconsistency was discovered during code review: `scripts/dev_loop.ps1`
and `scripts/dev_loop_prompt.md` query `category='stuck_dust'` for the
settled-check, but `cc_brain.py::sweep_dust()` was writing
`category='observation'` with `content.type='stuck_dust'`. The settled-check
was silently broken — the orchestrator could never see stuck_dust events.

Fix: both write sites in `sweep_dust()` now emit `category='stuck_dust'`
(with `pair=<asset>`) and the `content.type` redundant field is dropped.
Historical rows with the old shape are left as-is (no backfill needed).
The read side (`dev_loop.ps1`, prompts) was already correct and untouched.

## Owned paths

- `scripts/cc_brain.py`
- `scripts/dev_loop_prompt.md`
- `tests/test_cc_brain_untracked_sweep.py` (new)

## Out of scope

- No `import_asset_balance` flow creating rotation roots (deferred)
- No changes to `trading/reconciler.py` classification logic
- No changes to `runtime_loop.py` or other files
- No stablecoin-specific handling beyond the existing `_SKIP_BASES` skip

## Verification steps

1. `C:/Python313/python.exe -m pytest tests/ -x -q` — all green, count
   increases by 8 (new tests) from baseline of 725.
2. `C:/Python313/python.exe scripts/cc_brain.py --dry-run 2>&1 | grep -i "wallet\|FLOW\|TRIA\|untracked"` — shows FLOW and TRIA as sweep candidates (live smoke test, not part of automated verification).
