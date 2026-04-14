# Spec 34 -- Premature exit detector (Qullamaggie rule)

## Problem

The bot has no way to measure whether it is systematically selling winners
too early in trending markets. Qullamaggie's rule -- "you are never smarter
than the 10- and 20-day moving average" -- suggests an objective test: if
an exit happens while price is still above both EMA(10) and EMA(20) on the
relevant swing timeframe, it was premature.

Session 3 postmortem already showed child trades at 80% win rate but tiny
average profit ($1.60 net across 5 winners), while root trades at 22% WR
lost -$16.19. A detector that quantifies how many of those root exits
happened while the MAs were still bullish would tell us whether a trailing
EMA exit rule is worth the risk of changing live behavior.

This spec ships **detection only**. It does NOT change exit logic. The
orchestrator will pick up the signal on a later fire if the evidence
warrants it.

## Desired outcome

1. Running `python analysis/premature_exit.py --lookback-days 30` backfills
   every closed trade in `trade_outcomes` and tags each premature exit with
   a `cc_memory(category="premature_exit")` entry.
2. `scripts/cc_postmortem.py` calls the detector at the end of each run so
   new exits get tagged forward-going without a manual step.
3. Repeated runs are idempotent -- no duplicate memory rows for the same
   `trade_outcome_id`.
4. After initial backfill, the orchestrator can cheaply answer "how many
   premature exits in the last 14 days?" with a single `cc_memory` query.

## Premature exit criterion (v1)

An exit in `trade_outcomes` is classified **premature** iff all three hold:

1. `exit_price > EMA(10)` on 4H bars at exit time
2. `exit_price > EMA(20)` on 4H bars at exit time
3. `exit_reason` is NOT in `{"stop_loss"}`

Rationale: if price was above both 10- and 20-period EMAs when we sold,
Qullamaggie says we sold too early. We exclude `stop_loss` because those
were forced, not voluntary exits -- trailing-stop philosophy is a separate
discussion. All other reasons (`timer`, `take_profit`, `rotation`,
`root_exit_bearish`, `root_exit`, etc.) are voluntary and in scope.

4H is chosen because it matches the existing `MTF_4H_GATE` timeframe and
roughly corresponds to "swing" timeframe for crypto (vs daily for stocks).

## Data source

Use `research.ohlcv_cryptocompare.fetch_ohlcv_cryptocompare` for historical
bars. It already supports `since`/`until` parameters and Kraken as the
exchange. Only 60-minute interval is supported, so aggregate 1H -> 4H
in-memory.

For each exit:
- Fetch 1H bars from `(exit_ts - 30 days)` to `exit_ts` for the pair
- Aggregate to 4H bars using 4-bar UTC boundaries (00/04/08/12/16/20)
- Drop any partial/incomplete 4H bar that would extend past `exit_ts`
- Compute EMA(10) and EMA(20) on the 4H close series using the same
  iterative algorithm as `scripts/cc_brain.py:compute_ema`
- Require at least 20 4H bars for a valid classification; skip otherwise

## Acceptance criteria

1. **New package** `analysis/` with `__init__.py` (empty) and
   `premature_exit.py`.

2. **New module** `analysis/premature_exit.py` exports:
   - `detect_premature_exits(lookback_days: int, cc_memory: CCMemory,
     trade_outcomes: list[dict], *, dry_run: bool = False) -> dict`
     returning `{"scanned": N, "flagged": M, "skipped": K, "errors": E}`.
   - A `_classify(exit_price, ema10, ema20, exit_reason) -> bool` helper
     for unit testing the rule in isolation.
   - A `_aggregate_1h_to_4h(df_1h: pd.DataFrame) -> pd.DataFrame` helper.
   - A CLI entry point via `python -m analysis.premature_exit` or
     `python analysis/premature_exit.py` with flags:
     - `--lookback-days INT` (default 30)
     - `--dry-run` (print findings without writing memories)
     - `--bot-url STR` (default `http://127.0.0.1:58392`)
     - `--db-path STR` (default `data/bot.db`)

3. **CCMemory write schema** when a premature exit is detected:
   ```json
   {
     "category": "premature_exit",
     "pair": "<pair>",
     "importance": 0.7,
     "content": {
       "trade_outcome_id": <int>,
       "closed_at": "<iso>",
       "exit_reason": "<str>",
       "exit_price": "<decimal str>",
       "ema10_4h": "<decimal str>",
       "ema20_4h": "<decimal str>",
       "net_pnl": "<decimal str>",
       "rule_version": "v1"
     }
   }
   ```
   Use `cc_memory._write("premature_exit", content, pair=pair,
   importance=0.7)` directly since there is no typed helper for this
   category yet.

4. **Idempotency**: before writing, call `cc_memory.query(category=
   "premature_exit", hours=24*365, limit=10000)` and build a set of
   `trade_outcome_id` values already flagged. Skip writes for any id in
   that set. Re-running the backfill twice in a row must flag the same
   exits on pass 1 and write 0 on pass 2.

5. **Error handling**: if `fetch_ohlcv_cryptocompare` raises (network,
   unsupported pair, insufficient bars), log a warning and increment the
   `errors` counter. Do NOT crash the detector. Continue with the next
   exit. A single bad pair must not block the whole backfill.

6. **Integration with `scripts/cc_postmortem.py`**: at the end of `main()`
   (after the report is written), call
   `detect_premature_exits(lookback_days=30, cc_memory=CCMemory(...),
   trade_outcomes=outcomes)` wrapped in a broad try/except that logs but
   does not fail the postmortem if the detector errors. Print a one-line
   summary of the counts to stdout.

7. **Tests** in `tests/analysis/test_premature_exit.py`:

   a. `test_classify_premature_when_above_both_emas` -- exit price above
      EMA(10) and EMA(20), reason="timer" -> True.

   b. `test_classify_not_premature_when_below_ema10` -- exit price above
      EMA(20) but below EMA(10) -> False.

   c. `test_classify_not_premature_when_stop_loss` -- exit price above
      both EMAs, reason="stop_loss" -> False.

   d. `test_aggregate_1h_to_4h_boundary` -- pass 8 synthetic 1H bars with
      known timestamps on a 4H UTC boundary, assert 2 aggregated 4H bars
      with correct OHLC (first.open, max high, min low, last.close).

   e. `test_aggregate_1h_to_4h_drops_partial` -- pass 10 1H bars where
      the last 2 are in an incomplete 4H bucket; assert only complete
      4H bars are returned.

   f. `test_detect_writes_memory_for_premature_exit` -- use a stub
      `fetch_ohlcv_cryptocompare` returning bars where exit price is
      above both EMAs, one synthetic trade_outcome; assert one cc_memory
      row written with the correct fields.

   g. `test_detect_idempotent_on_rerun` -- run the detector twice on the
      same input; assert the second run writes zero new rows.

   h. `test_detect_skips_exits_with_insufficient_history` -- stub returns
      only 5 4H bars (< 20 required); assert the exit is skipped (not
      flagged, not errored).

   i. `test_detect_continues_on_per_pair_error` -- stub raises for pair
      A, returns valid data for pair B; assert B is still processed
      and the error counter reflects A.

   Tests MUST use a fresh in-memory `CCMemory(":memory:")` (sqlite3
   supports `:memory:` URIs). No network calls -- monkeypatch
   `fetch_ohlcv_cryptocompare`.

8. **Tests directory**: add `tests/analysis/__init__.py` (empty) so the
   test suite picks up the new package.

9. **Pytest clean**: `C:/Python313/python.exe -m pytest tests/ -x -q`
   passes with the new tests added. Current baseline is 688 tests; new
   count should be 697 (9 new).

## Out of scope

- **No live exit-logic change.** Do not touch `runtime_loop.py`,
  `trading/rotation_tree.py`, or any planner/executor path. This spec
  only observes and tags.
- **No orchestrator prompt change.** `scripts/dev_loop_prompt.md` is
  untouched by this spec. That integration happens in a follow-up once
  we have evidence from the backfill.
- **No alternative data source fallback.** If CryptoCompare is the wrong
  source long-term, that is a separate spec. For now it is the only
  library with `since`/`until` support already wired up.
- **No period alignment to 10/20 vs current 7/26.** cc_brain's entry
  EMAs stay at 7/26. This detector uses 10/20 specifically because that
  is the rule under test.

## Owned paths (Codex scope)

- `analysis/__init__.py` (new)
- `analysis/premature_exit.py` (new)
- `tests/analysis/__init__.py` (new)
- `tests/analysis/test_premature_exit.py` (new)
- `scripts/cc_postmortem.py` (edit: add detector call at end of `main()`)

## Verification (CC runs after Codex returns)

1. `C:/Python313/python.exe -m pytest tests/analysis -x -q` -- new tests pass
2. `C:/Python313/python.exe -m pytest tests/ -x -q` -- 697 total pass
3. `C:/Python313/python.exe analysis/premature_exit.py --lookback-days 30 --dry-run`
   -- runs without error against live bot, prints counts
4. `C:/Python313/python.exe analysis/premature_exit.py --lookback-days 30`
   -- writes memories
5. Second run of step 4 writes 0 new memories (idempotency)
6. `sqlite3 data/bot.db "SELECT COUNT(*) FROM cc_memory WHERE category='premature_exit'"`
   -- count matches the "flagged" number from step 4
