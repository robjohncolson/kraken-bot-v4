# Spec 36 -- DOGE/USD decision snapshot

## Problem

The user is starting fresh on trading strategy with a single-pair focus
(`DOGE/USD`). Their decision process today is manual and chart-driven:
glance at 24h color, then scan RSI/MACD/volatility across 1m/15m/1h/4h/1d
on Kraken Pro, then commit to one of three states -- 100% DOGE, 100% USD,
or 50/50 split.

That decision loop currently requires flipping between five chart
timeframes plus the wallet view, then weighing everything in the user's
head. There is no helper that lays the inputs out in one place, and there
is no pipeline for capturing the (inputs, decision) pairs as training
data for a future neural net.

## Desired outcome

A new CLI helper `scripts/doge_snapshot.py` that, in one invocation:

1. Fetches DOGE/USD OHLCV across all five timeframes from the existing
   bot REST endpoint.
2. Computes RSI(14) (Wilder's), MACD(12,26,9), and rolling volatility
   for each timeframe locally (no new dependencies).
3. Pulls current wallet balance + DOGE quantity from the bot.
4. Renders a compact terminal view showing 24h color, current price,
   holdings split, and a 5-row TF table (RSI, MACD line/signal/cross,
   histogram run, volatility).
5. Optionally logs the snapshot + the user's chosen decision
   (`DOGE` / `USD` / `SPLIT`) to `cc_memory` as a `doge_snapshot` row,
   so the (inputs, decision, eventual outcome) dataset builds itself
   from day one.

This spec ships **observation + journaling only**. It does not place
orders, suggest a state, or touch the bot/brain code paths.

## Acceptance criteria

### Part A -- `scripts/doge_snapshot.py` (new file)

1. **CLI flags**:
   - `--bot-url STR` -- default `http://127.0.0.1:58392`
   - `--pair STR` -- default `DOGE/USD` (single-pair only for now, but
     parameterized so future single-pair runs can reuse the script)
   - `--log {DOGE,USD,SPLIT}` -- when present, write a snapshot+decision
     row to `cc_memory` after rendering. Without this flag, the script
     is print-only.
   - `--note STR` -- free-text note attached to a logged row (ignored
     unless `--log` is also given).
   - `--json` -- emit a single JSON document instead of the human view.
     Mutually exclusive with terminal layout.
   - `--no-color` -- disable ANSI color escapes (for piping, dumb
     terminals, CI capture).

2. **Data pulled from the bot** (all via existing endpoints, no new ones):
   - `GET /api/exchange-balances` -- to derive DOGE qty and USD cash
     directly from Kraken (ground truth, not bot-tracked state).
   - `GET /api/ohlcv/<pair-encoded>?interval=I&count=200` for
     `I in (1, 15, 60, 240, 1440)`. The 200-bar window guarantees
     >= 35 bars for MACD(26+9) on every TF and >= 24 bars for the
     24h-change calc on the 1h series.
   - URL-encode `/` in the pair as `%2F` (existing convention --
     `cc_brain.py:fetch()` and `cc_brain.py:analyze_pair()` both do this).

3. **HTTP helper**: write a small `_fetch(endpoint: str) -> dict` using
   `urllib.request` (stdlib only, no `requests`). Match the error
   handling pattern of `scripts/cc_brain.py:fetch()` -- return
   `{"error": "<msg>"}` on failure, never raise to the caller.
   Timeout 30s.

4. **Indicator helpers** (all stdlib only -- `math.log`, `statistics.stdev`,
   pure Python lists). Do NOT import from `scripts.cc_brain` -- the
   existing `compute_rsi` there is the simple-SMA variant returning a
   scalar, and `compute_ema` returns only the latest value. This script
   needs series math:
   - `_ema_series(values: list[float], span: int) -> list[float]` --
     standard EMA with `alpha = 2/(span+1)`. Seed with `values[0]`.
     Returns a list the same length as input.
   - `_rsi_wilder(closes: list[float], period: int = 14) -> float` --
     Wilder's smoothing. First `avg_gain`/`avg_loss` are SMA over the
     first `period` deltas; subsequent values are
     `(prev * (period-1) + new) / period`. Returns the latest RSI
     (float in `[0, 100]`). If `len(closes) < period + 1`, return `50.0`.
     Wilder's is what TradingView (and therefore Kraken Pro) uses, so
     the printed value should match what the user sees on the chart.
   - `_macd(closes: list[float], fast: int = 12, slow: int = 26,
     signal: int = 9) -> dict` -- returns
     `{"line": [...], "signal": [...], "hist": [...]}` as full series
     (not just last values). MACD line = `ema(fast) - ema(slow)`,
     signal = `ema(MACD, signal)`, hist = `line - signal`.
   - `_hist_run(hist: list[float]) -> tuple[int, str]` -- returns
     `(count, color)` where `count` is the length of the trailing run of
     same-sign histogram bars and `color` is `"g"` for positive, `"r"`
     for negative, `"-"` if the latest hist is exactly zero or the list
     is empty. This implements the user's mental model: "many green bars
     on the hist".
   - `_macd_cross(line: list[float], signal_line: list[float]) -> str`
     -- returns `"up"` if `line[-2] <= signal_line[-2]` and
     `line[-1] > signal_line[-1]`, `"down"` if the reverse, else
     `"none"`. Looks only at the last two bars.
   - `_volatility_pct(closes: list[float], window: int = 14) -> float`
     -- rolling stdev of log returns over the last `window` returns,
     expressed as a percentage (`stdev * 100`). If fewer than `window+1`
     closes, return `0.0`.
   - `_24h_change_pct(closes_1h: list[float]) -> float` -- returns
     `(closes_1h[-1] / closes_1h[-25] - 1) * 100`. Uses 1h bars (24
     bars ago = 24h prior). If fewer than 25 bars, return `0.0`.

5. **Snapshot builder** `build_snapshot(bot_url: str, pair: str) -> dict`
   returning a structured dict:
   ```python
   {
       "pair": "DOGE/USD",
       "timestamp_utc": "<ISO 8601>",
       "price": <float>,                  # latest 1h close
       "change_24h_pct": <float>,
       "change_24h_color": "green"|"red"|"flat",
       "holdings": {
           "doge_qty": <float>,
           "doge_value_usd": <float>,     # qty * price
           "usd_cash": <float>,
           "doge_pct": <float>,           # 0..100
           "usd_pct": <float>,
       },
       "timeframes": {
           "1m":  {"rsi": <float>, "macd_line": <float>,
                    "macd_signal": <float>, "macd_cross": "up"|"down"|"none",
                    "hist_run": <int>, "hist_color": "g"|"r"|"-",
                    "vol_pct": <float>, "bar_count": <int>},
           "15m": {...},
           "1h":  {...},
           "4h":  {...},
           "1d":  {...},
       },
       "errors": [<str>, ...],            # any per-TF fetch failures
   }
   ```
   On a per-TF fetch failure, that TF entry must be `None` and the
   error message appended to `errors`. The function must not raise.
   If the balances fetch fails, set holdings to `None` and append the
   error -- do not crash.

6. **Renderer** `render_human(snapshot: dict, *, color: bool) -> str` --
   returns a multi-line string suitable for `print()`. Layout:
   ```
   DOGE/USD  -  2026-04-14 14:23:01 UTC
   Price: $0.12345    24h: +2.45%  GREEN
   Holdings: 250.50 DOGE ($30.92)  |  $19.08 USD  |  62% / 38%

   TF      RSI    MACD line   Cross   Hist     Vol%
   ------  ----   ---------   -----   ------   -----
   1m      52.3   +0.000123   ^       +5g      0.45
   15m     58.1   +0.000300   ^       +3g      0.82
   1h      61.4   +0.000800   -       +1g      1.45
   4h      65.2   +0.002100   v       -2r      2.31
   1d      72.4   +0.005000   v       -1r      4.55
   ```
   - `^` = MACD just crossed signal up, `v` = down, `-` = no recent cross
   - Hist column shows `<sign><run><color>` (e.g. `+5g`, `-2r`)
   - When `color=True`, color `GREEN`/`RED` text and the `^`/`v`/hist
     symbols using ANSI 16-color escapes (`\x1b[32m` green,
     `\x1b[31m` red, `\x1b[0m` reset). When `color=False`, emit no
     escape sequences.
   - When a TF entry is `None`, the row shows the TF label followed by
     `FETCH FAILED` and dashes for the metric columns.
   - When `holdings` is `None`, the holdings line shows
     `Holdings: unavailable`.

7. **JSON renderer** `render_json(snapshot: dict) -> str` -- returns
   `json.dumps(snapshot, indent=2, sort_keys=True)`.

8. **Logger** `log_decision(bot_url: str, snapshot: dict, decision: str,
   note: str | None) -> dict` -- POSTs to `/api/memory` with payload:
   ```json
   {
     "category": "doge_snapshot",
     "pair": "DOGE/USD",
     "importance": 0.7,
     "content": {
       "snapshot": <full snapshot dict>,
       "decision": "DOGE"|"USD"|"SPLIT",
       "note": "<str or null>",
       "schema_version": "v1"
     }
   }
   ```
   Returns the parsed response dict. Raises `ValueError` if `decision`
   is not one of `{"DOGE", "USD", "SPLIT"}`. Caller in `main()` catches
   and prints `error: ...` then exits 1.

9. **`main()`**:
   - Parse argv via `argparse`.
   - Build the snapshot.
   - If `--json`, print `render_json(snapshot)` and exit 0.
   - Else print `render_human(snapshot, color=not args.no_color)`.
   - If `--log` was given, after rendering call
     `log_decision(...)` and print one line: `logged: <decision>`
     (or `log failed: <reason>` and exit 1).
   - Exit code 0 on success, 1 if `build_snapshot` returned an empty
     `timeframes` dict (i.e. every TF failed).

10. **No new dependencies**. stdlib only:
    `argparse`, `json`, `math`, `statistics`, `sys`, `urllib.request`,
    `urllib.error`, `datetime`. No `requests`, `pandas`, `numpy`,
    `rich`, etc.

11. **No mutation of any other file**. `cc_brain.py`, `web/routes.py`,
    `persistence/cc_memory.py`, etc. are untouched. The new `doge_snapshot`
    memory category is created on first write -- the existing schema in
    `persistence/cc_memory.py` accepts arbitrary `category` strings.

### Part B -- Tests `tests/test_doge_snapshot.py` (new file)

12. **Unit tests for indicator helpers** (no network, pure math):

    a. `test_ema_series_length_and_first_value` -- input
       `[10, 11, 12, 13]`, span 3 -> output length 4, first value 10.0.

    b. `test_ema_series_known_smoothing` -- compare against a
       hand-computed reference for `[1.0, 2.0, 3.0, 4.0, 5.0]`, span 3.

    c. `test_rsi_wilder_neutral_on_flat_input` -- 50 closes all equal
       -> RSI = 50.0 (no gains or losses, function should return 50.0
       as the documented neutral fallback).

    d. `test_rsi_wilder_max_on_monotonic_up` -- 50 strictly increasing
       closes -> RSI very close to 100 (assert `> 99`).

    e. `test_rsi_wilder_min_on_monotonic_down` -- 50 strictly decreasing
       closes -> RSI very close to 0 (assert `< 1`).

    f. `test_rsi_wilder_short_input_returns_50` -- 5 closes -> 50.0.

    g. `test_macd_returns_three_series_of_same_length` -- 60 closes
       in -> `line`, `signal`, `hist` each length 60.

    h. `test_hist_run_counts_trailing_same_sign` -- input
       `[-1, -1, 1, 1, 1]` -> `(3, "g")`. Input
       `[1, -1, -1]` -> `(2, "r")`. Empty list -> `(0, "-")`.

    i. `test_macd_cross_up_down_none` -- three crafted inputs that
       exercise each branch.

    j. `test_volatility_pct_zero_on_constant` -- 50 equal closes -> 0.0.

    k. `test_volatility_pct_positive_on_random_walk` -- 50 closes with
       known returns, assert > 0 and finite.

    l. `test_24h_change_pct_uses_25th_to_last_bar` -- 30 closes, the
       25th-to-last is exactly half the latest, assert `~100.0`.

13. **Snapshot builder + renderer tests** (mock `_fetch` via
    `monkeypatch`, no real HTTP):

    m. `test_build_snapshot_calls_all_five_timeframes` -- monkeypatch
       `_fetch` to record endpoint hits and return synthetic 200-bar
       OHLCV; assert all of `interval=1`, `15`, `60`, `240`, `1440`
       were called and `snapshot["timeframes"]` has five entries.

    n. `test_build_snapshot_handles_per_tf_failure` -- monkeypatch
       `_fetch` to return `{"error": "boom"}` for the 4h request only;
       assert `snapshot["timeframes"]["4h"] is None` and the error
       string appears in `snapshot["errors"]`. Other TFs still populated.

    o. `test_build_snapshot_handles_balances_failure` -- monkeypatch
       `_fetch` to fail on `/api/exchange-balances`; assert
       `snapshot["holdings"] is None` and error logged. OHLCV TFs
       still populated.

    p. `test_render_human_runs_without_color` -- happy-path snapshot,
       `color=False`, assert returned string contains `DOGE/USD`,
       all five TF labels, and no `\x1b` escape characters.

    q. `test_render_human_with_color_includes_ansi` -- same, `color=True`,
       assert at least one `\x1b[` substring is present.

    r. `test_render_human_handles_missing_holdings` -- snapshot with
       `holdings=None`, assert string contains `Holdings: unavailable`
       and does not raise.

    s. `test_render_human_handles_failed_tf` -- snapshot with
       `timeframes["1m"] = None`, assert the row shows `1m` and
       `FETCH FAILED`.

    t. `test_render_json_emits_valid_json` -- `json.loads(render_json(snap))`
       round-trips.

14. **Logger tests**:

    u. `test_log_decision_posts_correct_payload` -- monkeypatch the
       internal POST helper; assert the payload has
       `category=="doge_snapshot"`, `pair=="DOGE/USD"`,
       `content["decision"] == "DOGE"`,
       `content["schema_version"] == "v1"`, and the full snapshot
       embedded under `content["snapshot"]`.

    v. `test_log_decision_rejects_invalid_value` --
       `log_decision(..., decision="MAYBE", ...)` raises `ValueError`.

    w. `test_log_decision_passes_through_note` -- `note="vibes"` ends
       up at `content["note"] == "vibes"`. `note=None` -> `null`.

15. **Pytest clean**: `C:/Python313/python.exe -m pytest tests/ -x -q`
    passes. The new tests add 22 cases to the suite. (Sonnet should
    record the post-merge total in the result file.)

## Out of scope

- No new REST endpoint -- everything is consumed via the existing
  `web/routes.py` API surface.
- No charts, no web UI, no TUI panel. Terminal only.
- No order placement, no trading actions, no changes to bot/brain code.
- No automatic decision suggestion. The user decides; the script just
  shows the inputs and journals the result.
- No backfill of historical decisions -- the dataset starts at zero and
  builds forward.
- No ML training, model architecture, or feature engineering. That is
  a separate spec once the dataset has enough rows.
- No multi-pair scan. This is single-pair (DOGE/USD) by design.
- No alternative volatility measures. v1 is rolling stdev of log
  returns; if it does not match the user's visual intuition once they
  see it, a follow-up swaps in ATR(14) or Bollinger width in one line.

## Owned paths (Sonnet scope)

- `scripts/doge_snapshot.py` (new)
- `tests/test_doge_snapshot.py` (new)

## Verification (CC runs after Sonnet returns)

1. `C:/Python313/python.exe -m pytest tests/test_doge_snapshot.py -v`
   -- 22 new tests pass.
2. `C:/Python313/python.exe -m pytest tests/ -x -q` -- full suite
   green; total count = previous baseline + 22.
3. `C:/Python313/python.exe scripts/doge_snapshot.py` -- prints a
   snapshot against the live bot at `127.0.0.1:58392`, exit 0.
4. `C:/Python313/python.exe scripts/doge_snapshot.py --no-color`
   -- same, no ANSI escapes in output (eyeball check).
5. `C:/Python313/python.exe scripts/doge_snapshot.py --json | python -m json.tool`
   -- JSON parses cleanly.
6. `C:/Python313/python.exe scripts/doge_snapshot.py --log SPLIT --note "spec 36 smoke test"`
   -- prints snapshot, then `logged: SPLIT`, exit 0. Verify with:
   `curl 'http://127.0.0.1:58392/api/memory?category=doge_snapshot&hours=1'`
   -- returns one row with `content.decision == "SPLIT"` and the note.
