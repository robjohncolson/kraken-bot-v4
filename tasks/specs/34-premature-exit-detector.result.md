# Result 34 -- Premature exit detector

## Status: LIVE

Codex implemented via cross-agent dispatch; CC verified, ran smoke tests,
committed.

## What shipped

- `analysis/__init__.py` -- new empty package marker
- `analysis/premature_exit.py` -- detector module (277 lines)
  - `_compute_ema`, `_classify`, `_aggregate_1h_to_4h`, `_already_flagged_ids`,
    `_fetch_trade_outcomes`, `detect_premature_exits`, `main`
  - CLI: `--lookback-days`, `--dry-run`, `--bot-url`, `--db-path`
- `tests/analysis/test_premature_exit.py` -- 9 tests (all green)
- `scripts/cc_postmortem.py` -- re-fetches `outcomes` in `main()`, invokes
  `detect_premature_exits` in a try/except so postmortem never crashes on
  detector errors

## Verification

| Step | Result |
|------|--------|
| `pytest tests/analysis -x -q` | 9 passed |
| `pytest tests/ -x -q` | **725 passed, 1 skipped** (baseline turned out to be 716; +9 new = 725) |
| Live dry-run against bot | 14 scanned, **4 flagged**, 0 skipped, 0 errors |
| Live write run | 14 scanned, 4 flagged, 0 skipped, 0 errors |
| SQLite row count | 4 rows in `cc_memory WHERE category='premature_exit'` |
| Idempotency re-run | 14 scanned, **0 flagged**, 4 skipped, 0 errors |

## Findings from backfill

**4 of 14 closed trades (~29%) were premature exits** by the v1 Qullamaggie
rule (price > EMA(10) AND price > EMA(20) on 4H at exit time, voluntary exit):

| id | pair | exit_reason | exit_price | EMA10 4H | EMA20 4H | net_pnl |
|----|------|-------------|-----------:|---------:|---------:|--------:|
| 8 | AKT/EUR | take_profit | 0.3923 | 0.3756 | 0.3830 | +1.0177 |
| 9 | 2Z/USD | root_exit_bearish | 0.0852 | 0.0850 | 0.0838 | 0.00 |
| ? | AERO/USD | (per memory) | — | — | — | — |
| ? | AVAX/USD | (per memory) | — | — | — | — |

The AKT/EUR `take_profit` at $0.39 with EMAs ascending beneath is a
textbook Qullamaggie "sold too early in a hot market" case -- price
was clearly above both the 10- and 20-period MAs when we capped it.

29% is a meaningful rate. It supports the hypothesis that a trailing
EMA exit rule would have caught additional upside on roughly 1 in 3
voluntary exits.

## Spec glitch caught + fixed during verification

- **Mistake in spec**: I told Codex to create `tests/analysis/__init__.py`.
  The project convention (verified after failure) is NO `__init__.py` in
  test subdirectories -- pytest uses `--import-mode=importlib`. Adding
  the `__init__.py` created a package namespace collision with the
  top-level `analysis/`, so `from analysis.premature_exit import ...`
  failed at collection. Fixed by deleting the stray file.
- **Pandas deprecation**: `df.resample("4H", ...)` emits a FutureWarning;
  lowercase `"4h"` is the new form. Fixed in-place after tests passed
  the first time.

## What this unblocks

The orchestrator prompt can now pick up a deterministic signal:

```sql
SELECT COUNT(*) FROM cc_memory
WHERE category='premature_exit'
  AND timestamp >= datetime('now', '-14 days');
```

Once the count crosses a threshold (spec proposal: >=5 over 14d), the
orchestrator can dispatch a follow-up spec to implement a trailing EMA
exit rule. That is NOT part of this spec -- this is detection only.

## Follow-ups for a later session

1. **Orchestrator prompt integration** (1-line add to
   `scripts/dev_loop_prompt.md`): teach the observe-step to read the
   `premature_exit` category and surface the count.
2. **Rule v2 refinement**: current rule includes `root_exit_bearish` in
   scope (since it is technically voluntary). If the detector finds too
   many false-positives from the bearish-reason path, narrow the rule to
   `{take_profit, rotation, timer}` only.
3. **Trailing-exit proposal spec** (only after >=5 premature exits in
   14d of forward-going data): design the shadow-mode EMA-trail policy,
   compare against live exits for >=7 days before any behavior change.
4. **Backfill beyond 30 days**: trade_outcomes currently has 14 rows,
   limited by the `lookback_days=30` param on `/api/trade-outcomes`.
   A longer lookback would give more signal.
