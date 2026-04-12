# Spec 12 — Permissions-aware pair blacklist

## Problem

On every brain cycle, the bot tries to exit AUD via `AUD/USD` and
Kraken rejects it:

```
EXIT: AUD via AUD/USD — hold_score=0.03 ($8.92, reason=quality_collapse)
FAILED: AUD/USD — Exchange error: EAccount:Invalid permissions:AUD/USD trading restricted for US:MA.
```

The bot has no memory of which pairs are permission-blocked, so it
re-attempts the same exit on every cycle (every 2 hours). This:

1. Wastes API calls
2. Pollutes brain reports with the same error
3. Leaves AUD/CAD/EUR/GBP/CHF/JPY positions stranded with no path
   to exit
4. Triggers other downstream re-evaluation that costs latency

The same restriction applies to other fiat pairs in
Massachusetts-regulated US accounts:
- `AUD/USD`, `CAD/USD`, `EUR/USD`, `GBP/USD`, `CHF/USD`, `JPY/USD`

The bot already has spec 03 (`fiat-filter-check-exits`) that filters
many of these out at the **proposal** step. But edge cases slip
through — quality_collapse exits, root deadline exits, etc.

The robust fix is **observation-based**: when Kraken returns
`EAccount:Invalid permissions`, cache the pair as untradeable so
nothing tries it again. This is a fail-safe regardless of which
proposal path picked the pair.

## Desired outcome

Once any pair returns an `EAccount:Invalid permissions` error, the
brain remembers it and skips it on future cycles — for both entries
and exits. The cache is persisted in `cc_memory` so it survives
restart.

## Acceptance criteria

1. A new `cc_memory` category `permission_blocked` (or equivalent) is
   written when an order placement returns
   `EAccount:Invalid permissions` from Kraken. The memory entry
   contains:
   - `pair` (e.g., `AUD/USD`)
   - `error_text` (the raw error string)
   - `first_blocked_ts` (ISO timestamp)
2. Before proposing an exit or entry order in `cc_brain.py`, the
   brain reads `permission_blocked` memories and removes the
   blocked pairs from the candidate list.
3. The check is keyed on the **trading pair**, not the asset, so
   `AUD/USD` blocked does not block `USD/AUD` (which is a different
   pair) — though in practice neither is supported.
4. The block is **permanent within a session**: once written, the
   memory is read on every subsequent cycle. There is no TTL — if
   Kraken later grants permissions, a manual delete is required.
   (Permissions changes are rare; TTL adds complexity for marginal
   benefit.)
5. A regression test: simulate an order failure with
   `EAccount:Invalid permissions`, verify the memory is written,
   verify the next cycle's candidate list does NOT contain the
   blocked pair.
6. The existing fiat filter from spec 03 remains in place — this
   spec adds a second layer of defense, not a replacement.

## Non-goals

- Do not implement automatic unblocking on Kraken permission
  changes — manual memory deletion is acceptable.
- Do not extend the blacklist to other error categories (e.g.,
  `EOrder:Insufficient funds` is a different bug). Only
  `EAccount:Invalid permissions`.
- Do not modify the bot side (`runtime_loop.py`) — the failure path
  is in the brain, fix it there. If the bot ever proposes the same
  exit autonomously (not in CC_BRAIN_MODE), follow up with a
  separate spec.
- Do not write a UI affordance for viewing/managing the blacklist.
  The TUI memory screen is sufficient.

## Acceptance test (manual)

1. Apply the patch.
2. Verify a fresh brain cycle still fires `AUD/USD` exit (because
   memory is empty).
3. Wait one cycle for the failure to be recorded.
4. Run `python scripts/cc_brain.py --dry-run` and confirm
   `AUD/USD` no longer appears in any "WOULD: ... AUD/USD ..." line
   even though the exit reason still applies.
5. Inspect `cc_memory` via SQLite:
   `SELECT * FROM cc_memory WHERE category='permission_blocked'` —
   one row for `AUD/USD`.

## Evidence

- `state/cc-reviews/brain_2026-04-12_1619.md` line "FAILED: AUD/USD"
- `scripts/cc_brain.py` lines 1467-1470 (failure logging path that
  needs to also write the memory)
- Memory schema: `persistence/cc_memory.py` (`cc_memory` table)
