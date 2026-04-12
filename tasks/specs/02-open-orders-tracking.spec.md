# Spec 02 — Open-orders tracking via bot API

## Problem

The `pending_order` memory system only tracks orders the brain
**successfully placed and remembered**. If an older run placed an order
and then crashed or was restarted before writing the memory, the brain
has no record that the order exists. The next cycle re-proposes the
same trade and fails with `EOrder:Insufficient funds` because the
exchange is already holding the balance for the ghost order.

Evidence: AUD/USD sell attempts have failed 5× in a row over 6 hours
with no corresponding `pending_order` memory entries. Exchange balance
shows `AUD avail=12.6357 held=0.0000`, yet Kraken still rejects the
sell. Most likely cause: an earlier pending sell order from before
the current pending-order tracking was wired up, still sitting open
on Kraken but invisible to the brain.

## Desired outcome

The brain has a live, authoritative view of its open orders on Kraken.
The decision step consults this view, not the memory-based proxy, when
deciding whether to re-propose a trade on a pair.

## Acceptance criteria

1. A new GET endpoint `/api/open-orders` returns the current list of
   open orders on Kraken for the bot's account, with per-order:
   `txid`, `pair`, `side`, `volume`, `price`, `status`, `opentm`
   (open timestamp).
2. `scripts/cc_brain.py` has a helper
   `get_pairs_with_open_orders() -> set[str]` that queries this
   endpoint and returns the set of pair names that currently have an
   open order on the exchange.
3. Step 5 of cc_brain uses `get_pairs_with_open_orders()` as the
   primary source of blocked pairs, unioned with (not replaced by)
   the existing `get_pairs_with_pending_orders()` memory-based set.
4. `check_pending_orders()` is extended: if an order has been open
   longer than `STALE_ORDER_HOURS` regardless of whether the brain
   has a memory entry for it, the function cancels it via the
   existing DELETE endpoint.
5. A dry-run followed by a live cycle demonstrates that AUD/USD (or
   whatever pair currently has stale ghost orders) no longer appears
   as a failed sell attempt.

## Non-goals

- Do not add a new WebSocket feed or polling loop; rely on the existing
  Kraken HTTP adapter.
- Do not redesign the memory system; keep `pending_order` memories as
  a secondary source.
- Do not touch the trade history or fills endpoint.

## Evidence

- `state/cc-reviews/brain_2026-04-12_*.md` — multiple `FAILED: AUD/USD
  — Exchange error: EOrder:Insufficient funds` lines.
- `/api/memory?category=decision&hours=6` — 5 AUD/USD sell decisions
  over 6h, none with a txid (meaning all placement attempts failed).
- `/api/memory?category=pending_order&hours=6` — returns 0 results.
