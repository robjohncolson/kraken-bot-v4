# Spec 06 — Backfill script fidelity fix

## Problem

`scripts/backfill_shadow.py` produces misleading shadow-vs-live comparisons
because it conflates three fundamentally different kinds of cycles:

1. **Dry runs** — cycles where I ran `python scripts/cc_brain.py --dry-run`
   during development. These contain `Mode: DRY RUN` and their Step 6 shows
   `WOULD: buy ...` lines. No real order was ever placed.
2. **Live attempts that failed** — cycles where the bot tried to place an
   order and Kraken rejected it (`FAILED: X/USD — Exchange error: ...`).
   Common reasons: ordermin, pair_decimals, insufficient funds. No trade
   occurred.
3. **Live fills** — cycles that actually resulted in `PLACED: X/USD txid=...`
   and the bot took a real position.

The current script reads `ENTRY from USD: X/USD` from Step 5 as "live's
decision" without checking Step 6. It then fetches X's forward return and
attributes that return to live's column, regardless of whether the trade
actually happened. For shadow comparison this is fatally wrong: the bot
never captured the +35.25% RAVE move because the order was rejected, but
the backfill credited live with +35.25% anyway, flipping the aggregate
shadow-vs-live edge from strongly positive to slightly negative.

Corrected analysis on the 6 actually-filled cycles in the same window:
shadow wins 6/6, cumulative edge +11.59% (shadow over live).

## Desired outcome

The backfill script only evaluates cycles where a real order placed and
filled on Kraken. Dry runs and failed attempts are explicitly excluded,
and the summary reports the filter counts so the user can see what was
dropped.

## Acceptance criteria

1. `scripts/backfill_shadow.py` parses `Mode: DRY RUN` or `Mode: LIVE`
   from the top of each report and skips `DRY RUN` cycles entirely.
2. For `LIVE` cycles, the script parses Step 6 and only counts cycles
   that contain `PLACED: <pair> txid=<txid>`. Cycles with `FAILED:` or
   no Step 6 action line are dropped.
3. The summary output includes a filter report:
   ```
   Reports scanned:      54
   Dry-run cycles:       NN (dropped)
   Failed-order cycles:  NN (dropped)
   Filled-order cycles:  NN (kept for analysis)
   ```
4. The per-cycle table only contains filled cycles.
5. The aggregate metrics (wins, cumulative edge, etc.) are computed from
   filled cycles only.
6. Running the updated script on the current state/cc-reviews/ must
   produce a different cycle count and different aggregate numbers than
   the pre-fix run. Specifically: the RAVE/USD 2026-04-12 01:32 cycle
   must be excluded (failed order).
7. The 2h-window analysis remains available via `--forward-hours 2`.

## Non-goals

- Do not change the reconstruction logic (score_entry recovery from logs).
  That still needs to run against all cycles' analysis data.
- Do not change the output format beyond adding the filter report and
  trimming to filled cycles.
- Do not touch the shadow-mode runtime logic in `cc_brain.py`.
- Do not modify `tasks/specs/05-backfill-6h-analysis.result.md` (it was
  correct relative to the buggy script; regenerating is a separate task).

## Evidence

- `state/cc-reviews/brain_2026-04-12_0132.md` — shows `FAILED: RAVE/USD`
  in Step 6 despite `ENTRY from USD: RAVE/USD` in Step 5. Current backfill
  scores this as live's +35.25% win.
- `state/cc-reviews/brain_2026-04-11_2249.md` — shows `Mode: DRY RUN` and
  `WOULD: buy ...` in Step 6. Current backfill treats this as a live
  decision.
- Manual count from a grep across `brain_2026-04-1[12]_*.md`:
  19 total `ENTRY` lines in the backfill window → 10 DRY RUN, 3 FAILED,
  6 PLACED.
