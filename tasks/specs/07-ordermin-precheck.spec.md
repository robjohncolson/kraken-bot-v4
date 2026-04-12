# Spec 07 — Ordermin + costmin pre-check in planner

## Problem

The bot proposes trades without checking Kraken's minimum order size
(`ordermin`, measured in the base asset) and minimum cost (`costmin`,
measured in the quote asset). When the proposed quantity is below those
minimums, Kraken rejects the order with errors like:

- `EGeneral:Invalid arguments:volume minimum not met` (ordermin)
- `EGeneral:Invalid arguments:cost minimum not met` (costmin)

Evidence:
- `state/cc-reviews/brain_2026-04-12_0132.md`: RAVE/USD rejected with
  `volume minimum not met`. The bot had a `$MAX_POSITION_USD` budget of
  ~$28 but RAVE/USD's `ordermin` would have required more.
- Earlier reports show similar ordermin rejections across multiple pairs.

Each rejection is a wasted analysis cycle: the bot spent GPU time scoring
the pair, selected it as the top candidate, built the order, sent it to
Kraken, and got rejected. A simple pre-check would have eliminated these
pairs from consideration entirely.

The user's existing memory note flags this exact issue: "Kraken minimum
order sizes — Fetch ordermin from AssetPairs API, enforce in planner to
stop wasted retries." The ordermin field is already stored in
`_fetch_kraken_pairs()` (via commit for pair_decimals); we just need to
actually use it.

## Desired outcome

Before the bot proposes a buy (entry or rotation) or a sell (exit), it
verifies that:
- The computed quantity meets `ordermin` for the pair
- The computed cost meets `costmin` for the pair

Pairs that fail either check are excluded from the candidate pool at the
analysis stage, not at the order-placement stage.

## Acceptance criteria

1. `_fetch_kraken_pairs()` stores `ordermin` and `costmin` (both as
   floats) in the cached pair info dict, alongside the existing
   `pair_decimals` and `lot_decimals`.
2. A new helper `_meets_order_minimums(pair: str, qty: float, price: float) -> tuple[bool, str]`
   returns `(True, "")` or `(False, reason)` based on the stored minimums.
3. `evaluate_portfolio()` filters rotation proposals: any rotation whose
   sell-side quantity fails the minimums is dropped with a log line.
4. Step 5b filters entry candidates: before the `scored.sort(...)` line,
   candidates whose `max_position_value / price` falls below ordermin
   are filtered out with a log entry.
5. Step 5c filters exits: a held position that can't meet ordermin on
   its own quantity is skipped (with a "stuck below ordermin" log) rather
   than generating a doomed sell order.
6. A dry-run cycle demonstrates: when the bot would have proposed a pair
   that fails the minimum, it now logs the skip and picks the next best
   pair instead.
7. No live orders fail with `volume minimum not met` or `cost minimum not met`
   errors in the next ~5 cycles after this lands.

## Non-goals

- Do not change the scoring logic for pairs that DO meet minimums.
- Do not auto-increase position size to meet minimums — if the budget is
  too small for the pair, just skip the pair.
- Do not retry orders after rejection — the pre-check prevents them from
  being sent in the first place.
- Do not touch the dust sweep path; its use case is "sell everything
  even if sub-minimum" and is handled separately.

## Evidence

- `state/cc-reviews/brain_2026-04-12_0132.md` — `FAILED: RAVE/USD` on
  `volume minimum not met`
- `_fetch_kraken_pairs()` in `scripts/cc_brain.py` — already has access
  to `meta.get("ordermin")` via Kraken AssetPairs response
- User memory `project_minimum_order_sizes.md`: "Fetch ordermin from
  AssetPairs API, enforce in planner to stop wasted retries"
