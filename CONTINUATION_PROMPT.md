# Continuation Prompt — kraken-bot-v4

## Architecture (local-first, decided 2026-03-24)

- **Spare laptop at home**: single always-on runtime host
- **Kraken**: truth for live balances/orders/fills
- **SQLite** (`./data/bot.db`): durable coordination store (WAL mode)
- **Local JSONL**: audit/recovery trail (ledger, snapshots, offline queue)
- **FastAPI dashboard**: local on bot host (`localhost:58392`)
- **Tailscale**: remote access from school
- **No Supabase** in runtime path (legacy code retained, not used)
- **No Railway** (dashboard is local)

## Current state

Two benchmark lines exist, with explicit source provenance:

**Kraken-native 30d hourly benchmark** (primary, `data/research/`, source: Kraken REST OHLC):
- V1 baseline: -3,103 bps, 44.1% accuracy, Sharpe -22.6 (4-fold, 10d train / 1d val / 5d step, 693-row post-dedup dataset)
- No profitable candidate on this short window. Momentum (A) has the best corrected lift (+2,032 bps vs V1).

**CryptoCompare-backed 180d hourly benchmark** (separate track, `data/research-cc-180d/`, source: CryptoCompare `e=Kraken`):
- V1 baseline: **+5,531 bps**, 47.2% accuracy, **Sharpe 11.3** (18-fold, 90d train / 1d val / 5d step, 4,320-row dataset)
- Momentum (A): -52 bps. Combined (E): -3,291 bps. **Feature engineering hurts on the longer window.**
- V1 is the clear winner — simpler features resist overfitting with more training data.

**30-day overlap validation (passed)**: CryptoCompare `e=Kraken` vs Kraken-native on the same 693-row window shows close max diff 0.05%, volume mean diff 0.004%, perfect timestamp alignment (693/693). Source effect on V1 P&L: -303 bps (~10% of 30d loss) — small, not the driver.

**Key conclusions**:
- The 30d→180d swing (+8,937 bps) is driven by the longer training window, not the data source.
- V1 with 90d training data is profitable and outperforms all feature-enhanced candidates.
- Momentum and combined features should be deprioritized — they overfit on short windows.
- Cross-TF is deprioritized: 75% of its hourly lift was lookahead artifact (fixed 2026-03-29), and it does not transfer to 4h.
- **Research winner (180d CC-backed track): V1 uncalibrated** — +5,531 bps, Sharpe 11.3, 53.7% hit rate, 214 trades.
- Isotonic calibration rejected: edge collapsed under TS-safe evaluation (+1,565 bps vs +5,531 uncalibrated). Non-TS-safe result (+11,479) was a calibration artifact.
- Platt scaling also rejected: destroys trading performance (-2,326 bps).

Other:
- Historical pre-dedup Phase 5a result (+2,838 bps) was on a dataset with duplicate timestamps — preserved below.
- LLM infrastructure works but does not outperform LogReg. Phase 5b paused.
- Start work from `master` branch.

## Research conclusion (accepted 2026-03-29)

**V1 uncalibrated logistic regression is the accepted research winner** on the 180d CryptoCompare-backed hourly track.

- **What won**: 7-feature V1 LogReg (ret_1, ret_6, ret_12, hl_range, co_range, vol_ratio, volatility). LogisticRegression(max_iter=1000, C=1.0, random_state=42), StandardScaler, threshold 0.55.
- **No feature additions**: Every feature family tested (momentum, vol/regime, volume, cross-TF) degraded performance on the longer 180d window. Feature engineering overfits on short training windows.
- **No calibration**: Platt scaling destroyed trading performance. Isotonic looked good under non-time-aware inner CV (+11,479 bps) but collapsed to +1,565 bps under TS-safe tail-holdout calibration — losing 12 of 18 folds. The simpler model wins.
- **Source provenance**: CryptoCompare `histohour` with `e=Kraken`. 30-day overlap validation PASSED (close <0.05% diff, 693/693 timestamps). This does NOT replace the Kraken-native 30d line — both are maintained.
- **Artifact**: `artifacts/logistic_regression_20260329_3f73bb8a/` (promoted, manifest includes `data_source.source=cryptocompare`, `data_source.exchange=Kraken`, overlap validation result).

## Integration status (completed 2026-03-29)

**Shadow mode is wired and ready to run.** TA ensemble remains primary. Research model runs in parallel, logging predictions (including raw `prob_up`) without affecting the reducer or trading decisions.

### Artifact

| Field | Value |
|-------|-------|
| Artifact ID | `logistic_regression_20260329_3f73bb8a` |
| Model family | `logistic_regression` v1.0 |
| Features | 7 V1 features (ret_1, ret_6, ret_12, hl_range, co_range, vol_ratio, volatility) |
| Threshold | 0.55 (LONG if prob_up > 0.55, SHORT if < 0.45, else ABSTAIN) |
| Calibration | none |
| Data source | CryptoCompare `e=Kraken` (overlap validated) |
| Training data | 4,314 rows from 180d CC-backed dataset |
| Model files | `artifacts/.../model/scaler.pkl`, `model.pkl`, `meta.json` |

### Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `BELIEF_MODEL` | `technical_ensemble` | Primary belief handler (`technical_ensemble` or `research_model`) |
| `ACTIVE_ARTIFACT_ID` | none | Required when `BELIEF_MODEL=research_model` |
| `SHADOW_ARTIFACT_ID` | none | Run research model in shadow mode (logs only, no reducer impact) |

### Operator workflow

```bash
# Default: TA ensemble only
python main.py

# Shadow run (current recommended mode):
SHADOW_ARTIFACT_ID=logistic_regression_20260329_3f73bb8a python main.py

# Full swap (DO NOT USE YET — pending shadow validation):
# BELIEF_MODEL=research_model ACTIVE_ARTIFACT_ID=logistic_regression_20260329_3f73bb8a python main.py
```

Shadow predictions appear in logs as:
```
shadow_prediction: pair=DOGE/USD direction=bearish confidence=0.1064 prob_up=0.4468 artifact=logistic_regression_20260329_3f73bb8a
```

### Shadow evaluation

Run `python -m research.shadow_eval --log-file <path>` to compute daily metrics from shadow logs:
- Prediction coverage (% of poll cycles that produced a prediction)
- Abstain rate
- 6h directional accuracy (matched against actual OHLCV outcomes)
- Paper P&L and hit rate

### Rollout gates (before full swap)

| Gate | Threshold |
|------|-----------|
| No crashes / malformed outputs | Over full shadow period |
| Stable belief cadence | >90% of poll cycles produce a prediction |
| Shadow directional accuracy | >50% on live data over 1+ weeks |
| No reconciliation / runtime regressions | Over shadow period |

### New files added

| File | Purpose |
|------|---------|
| `beliefs/research_model_source.py` | Generic artifact loader (BeliefAnalyzer protocol) |
| `beliefs/research_model_handler.py` | Handler + shadow handler factories |
| `research/ohlcv_cryptocompare.py` | CryptoCompare `e=Kraken` OHLCV fetcher |
| `research/shadow_eval.py` | Shadow log evaluation utility |
| `tests/test_research_model.py` | 10 tests for source + handler |

### Tests

457 total (447 existing + 10 new), zero failures.

## Active phase: Shadow run (started 2026-03-29)

**Default launch command:**
```bash
SHADOW_ARTIFACT_ID=logistic_regression_20260329_3f73bb8a python main.py
```

TA ensemble is primary. Research model logs shadow predictions only. Do not full-swap yet.

**Daily evaluation:**
```bash
python -m research.shadow_eval --log-file <path-to-bot-log>
```

Uses paginated `fetch_ohlcv_history` for outcome matching — covers the full shadow window (Kraken retains ~30 days of hourly data). Each prediction is matched against the actual close 6h later.

**No further feature work, calibration, or model changes until shadow validation completes.**

## Conditional rotation wiring fixes (completed 2026-03-30)

Codex code review identified 3 HIGH and 2 MEDIUM gaps in the Layer 1-3 implementation. All HIGH issues fixed:

1. **ClosePosition self-contained** — enriched with pair/side/quantity/limit_price so `_execute_close_position` no longer looks up already-mutated state. All emitters (position lifecycle, risk rules) populate fields.
2. **Tree binding deferred to post-reducer** — `_maybe_bind_tree_to_position()` runs in `run_once()` after scheduler cycle, when the position actually exists in portfolio. Sets `position_id`, `opened_at`, and recomputes `expires_at` from actual open time.
3. **Planner filters held pairs** — `_select_candidate()` excludes pairs with existing positions or pending orders, preventing guardian from accidentally expiring unrelated positions.
4. **Tree cleared on any tracked exit** — scheduler clears `conditional_tree_state` on `StopTriggered`/`TargetHit` when `position_id` matches, not just on `WindowExpired`.
5. **Risk rules enriched** — hard-drawdown `CloseAllPositions` now populates full `ClosePosition` fields.

Known non-blocking caveats (acceptable for initial deployment):
- `opened_at` uses cycle time (`utc_now()`) not exact fill timestamp (~30s drift on 6-24h windows)
- Exit limit price is still entry_price (pre-existing placeholder)
- Candidate exclusion uses bot_state only, not reconciled exchange state (restart edge case)

484 tests pass, ruff clean.

## Goal for next session

Review shadow metrics after 1+ week. If rollout gates pass, decide: tiny-size live rollout ($10 max position) or continue shadow.

## Completed priorities

1. ~~**Resolve benchmark parity**~~ — dataset dedup was the sole cause
2. ~~**Feature engineering ablation**~~ — V1 wins on long window, features overfit
3. ~~**Cross-TF lookahead fix**~~ — timestamp-aligned, lagged by 1 block
4. ~~**4h robustness study**~~ — momentum transfers, cross-TF does not
5. ~~**Temporal leakage audit**~~ — all clean except the fixed cross-TF bug
6. ~~**Longer hourly evaluation**~~ — 180d via CryptoCompare e=Kraken; V1 wins
7. ~~**30-day overlap validation**~~ — PASS (close <0.05%, source effect ~300 bps)
8. ~~**Probability calibration**~~ — Platt and isotonic both rejected; V1 uncalibrated wins
9. ~~**Artifact promotion**~~ — V1 promoted with source provenance
10. ~~**Integration**~~ — generic artifact loader, shadow mode, env vars, 457 tests pass
11. ~~**Conditional rotation wiring**~~ — 5 fixes from CC+Codex review, 484 tests pass

## Remaining priorities

1. **Shadow/paper mode** validation on live data before real trading
2. ~~**Fix TA ensemble**~~ — RESOLVED: v1.1 in autoresearch already stores training tail and prepends it in predict(). The Phase 5a 0-trade result was v1.0 (pre-fix, 24-bar val window < 40-bar minimum). v1.1 produces 85 trades (693-row) and 400 trades (1-day-step). Live bot path is unaffected (fetches 50 bars, DOGE/USD always has data).
3. ~~**Exit price improvement**~~ — RESOLVED: all close paths now pass trigger/reference price as exit_price. Runtime applies configurable marketable-limit offset (EXIT_LIMIT_OFFSET_PCT, default 0.1%) and quantizes to min 4dp. Covers stop, target, window expiry, belief change, and hard drawdown. 488 tests pass.
4. **Revisit LLM only** with fundamentally different inputs (news/sentiment, not raw OHLCV)
5. **Run TA on 180d CC-backed dataset** — no autoresearch experiment exists for the 4,320-row dataset yet

## Validation steps

After changes, run and report:
- `pytest` in autoresearch (all tests green)
- Backtest via `python -m trading_eval.cli run` — before/after metrics for modified candidates
- `pytest` in kraken-bot-v4 (447+ tests passing)
- Dashboard smoke check (`localhost:58392`) or TUI (`python -m tui`)

Report: what changed, before/after metrics, any risks or follow-up work.

### TUI Operator Cockpit (v1, completed 2026-03-26)

**Launch**: `python -m tui` (or `TUI_BASE_URL=http://host:port python -m tui`)

**Package**: `tui/` — fully isolated from `web/` and runtime, consumes existing dashboard API + SSE.

Screens (keyboard-navigable):
- `1` Overview (health, portfolio, positions, orders, beliefs, reconciliation, event log)
- `2` Positions (full table with pair/side/qty/entry/stop/target/price/P&L/grid)
- `3` Beliefs (matrix by pair and source with direction/confidence/regime)
- `4` Orders (open + pending orders)
- `5` Reconciliation (discrepancy/ghost/foreign/untracked/fee drift)
- `6` Event Log (recent bot events, ring buffer)
- `?` Help (key bindings + color legend)
- `r` manual refresh, `p` pause/resume, `[`/`]` pair navigation, `q` quit

Data flow: initial snapshot from `/api/*` endpoints, live updates from `/sse/updates` with exponential backoff reconnect. SSE disconnect shows degraded banner, never crashes.

No backend changes required — the TUI consumes the existing read model. If future TUI features need new fields (pending orders detail, cooldowns, heartbeat summary), extend the shared dashboard read model first.

Tests: 54 new tests (state parsers, SSE parser, theme helpers, Textual app navigation). 447 total passing.

### Completed phases

- **Phase 0**: renamed autoresearch → technical_ensemble
- **Phase 1**: research dataset export in kraken-bot-v4 (`research/` module)
- **Phase 2**: walk-forward evaluation harness in autoresearch (`trading_eval/` package)
- **Phase 3**: baselines established (TA ensemble, logistic regression, GBT)
- **Phase 4**: artifact contract defined, synced to kraken-bot-v4
- **Phase 5a**: LLM candidate evaluated — logistic regression wins (see below)

### Phase 2 deliverables (in autoresearch)

- `trading_eval/config.py` — EvalConfig dataclass
- `trading_eval/data.py` — manifest-validated Parquet loader
- `trading_eval/splitter.py` — walk-forward time-series splitter
- `trading_eval/candidate.py` — Candidate ABC with timestamp-keyed predictions
- `trading_eval/backtest.py` — backtest engine with fees, slippage, abstain
- `trading_eval/metrics.py` — direction accuracy, Brier, P&L, Sharpe, drawdown
- `trading_eval/runner.py` — experiment orchestration
- `trading_eval/storage.py` — structured experiment records with reproducibility metadata
- `trading_eval/artifact.py` — artifact schema and promotion workflow
- `trading_eval/cli.py` — CLI: run, list, compare, promote, artifacts
- `trading_eval/baselines/ta_ensemble.py` — standalone 6-signal TA port
- `trading_eval/baselines/sklearn_baseline.py` — LogReg + GBT baselines
- `trading_eval/baselines/run_baselines.py` — baseline runner + comparison
- 102 tests passing, 11 parity tests (skip on Python 3.10)

### Phase 4 deliverables

- `trading_eval/artifact.py` — ArtifactManifest + promote_candidate()
- `kraken-bot-v4/docs/specs/artifact-contract-v1.md` — consumer interface spec

### Phase 5a result — historical, pre-dedup (evaluated 2026-03-26)

**Dataset**: 721-row DOGE/USD hourly OHLCV (manifest hash `ffe3cdacc876b51a`). This dataset contained 28 duplicate timestamps from Kraken API pagination boundary overlaps, fixed in commit `a584999`.

Walk-forward results (5-fold, 10d train, 1d val, 5d step, DOGE/USD):

| Candidate | Accuracy | Net P&L (bps) | Sharpe | Hit Rate | Trades |
|-----------|----------|---------------|--------|----------|--------|
| Logistic regression | 67.6% | +2,838 | 29.6 | 63.2% | 68 |
| LLM (Qwen3 8B) | 62.5% | -167 | -9.3 | 50.0% | 8 |
| GBT | 44.3% | -2,732 | -19.5 | 43.2% | 88 |
| TA ensemble | 0% | 0 | 0 | 0% | 0 |

**Verdict**: Prompted LLM does not beat logistic regression. Infrastructure is proven (structured output works, GPU path viable, contract enforced), but there is no signal advantage. TA ensemble v1.0 produced 0 trades (needs 40 bars history, 24-bar validation window too short). Fixed in v1.1 (training tail prepended) — subsequent runs produce 85-400 trades.

**Note**: The +2,838 bps logistic regression result is **superseded** — it was achieved on a dataset with duplicate timestamps. See "Benchmark parity resolution" below for the corrected result.

### Benchmark parity resolution (2026-03-28)

**Root cause**: The Phase 5a canonical result (+2,838 bps) and the ablation control (-3,103 bps) used different datasets. Commit `a584999` fixed OHLCV timestamp deduplication (Kraken API pagination overlap), which changed the dataset from 721 rows to 693 rows.

| | Pre-dedup (Phase 5a) | Post-dedup (corrected) |
|---|---|---|
| Manifest hash | `ffe3cdacc876b51a` | `90ec69cafddba724` |
| Row count | 721 (28 duplicates) | 693 |
| Date range | 2026-02-24 11:00 — 2026-03-26 11:00 | 2026-02-26 03:00 — 2026-03-26 23:00 |
| Walk-forward folds | 5 (fold 4 degenerate: 1 val row, 0 trades) | 4 |
| V1 LogReg P&L | +2,838 bps | -3,103 bps |

The dedup shifted the dataset start by +40 hours, completely changing which market data falls into each fold's train/val window. The 5th fold in the old dataset was degenerate (1 validation row, 0 trades). Config, model hyperparameters, preprocessing, and seeds are identical across both runs.

**Conclusion**: The corrected 693-row dataset is the canonical dataset. The old +2,838 bps result is a historical artifact of duplicate data.

### Current benchmark — post-dedup V1 baseline (2026-03-28)

**Dataset**: 693-row DOGE/USD hourly OHLCV (manifest hash `90ec69cafddba724`)
**Config**: 4-fold walk-forward, 10d train / 1d val / 5d step, fee 10 bps, slippage 5 bps
**Model**: LogisticRegression(max_iter=1000, C=1.0, random_state=42), StandardScaler, threshold 0.55

| Metric | Value |
|--------|-------|
| Direction accuracy | 44.1% |
| Net P&L | -3,103 bps |
| Sharpe | -22.6 |
| Hit rate | 44.1% |
| Trades | 68 |
| Brier | 0.343 |
| Max drawdown | 4,846 bps |

This is the canonical V1 reference for all feature ablation comparisons.

### Feature engineering ablation — corrected (2026-03-28)

V1 baseline feature pipeline (7 features):
- `ret_1`: 1-bar close return
- `ret_6`: 6-bar close return
- `ret_12`: 12-bar close return
- `hl_range`: `(high - low) / close`
- `co_range`: `(close - open) / open`
- `vol_ratio`: `volume / rolling_mean(volume, 20)`
- `volatility`: `rolling_std(returns, 12)`

Feature families tested (additive to V1):
- **Momentum (+4)**: Williams %R 14, Stochastic K 14, ROC 3, ROC 24
- **Vol/Regime (+4)**: Garman-Klass vol 12, vol percentile rank 24, vol ratio 6/24, range percentile rank 24
- **Volume (+4)**: OBV slope 12, VWAP distance 24, A/D line slope 12, vol-price correlation 12
- **Cross-Timeframe (+3)**: 4h aggregated return, 12h aggregated return, 4h volume ratio

**Cross-TF lookahead bug (found and fixed 2026-03-29)**: The original cross-TF feature builder (`_build_features_ablation_cross_tf`) had a future information leak. It grouped bars into 4h/12h blocks using `np.arange(n) // 4`, then assigned the block's final close and total volume to ALL rows in the block — including early rows that would not yet know those values at decision time. Blocks were also index-anchored, not timestamp-aligned. Fix: use `timestamp // 14400` for alignment, lag values by 1 block so each row only sees the previous completed block. This affected candidates D, F, and E.

Ablation results — **corrected** (4-fold walk-forward, 10d train / 1d val / 5d step, 693-row corrected dataset):

| Candidate | Trades | Accuracy | Net P&L (bps) | Sharpe | Brier | MaxDD |
|-----------|--------|----------|---------------|--------|-------|-------|
| V1 baseline (control) | 68 | 44.1% | -3,103 | -22.6 | 0.343 | 4,846 |
| V2 (18 features) | 88 | 40.9% | -4,417 | -21.6 | 0.441 | 7,202 |
| A: +momentum | 90 | 50.0% | -1,071 | -5.2 | 0.333 | 3,928 |
| B: +vol/regime | 77 | 44.2% | -2,807 | -16.3 | 0.324 | 4,778 |
| C: +volume | 83 | 42.2% | -2,107 | -11.4 | 0.370 | 5,337 |
| D: +cross-timeframe | 71 | 46.5% | -1,980 | -13.8 | 0.340 | 3,926 |
| F: +cross-tf + momentum | 88 | 50.0% | -1,416 | -7.1 | 0.337 | 3,847 |
| E: combined (all 4) | 88 | 54.5% | -192 | -0.9 | 0.335 | 4,809 |

Lookahead impact (D, F, E — before vs after fix):

| Candidate | Leaky P&L | Fixed P&L | Leak artifact | Fixed lift vs V1 |
|-----------|-----------|-----------|---------------|------------------|
| D: +cross-tf | +1,390 | -1,980 | -3,370 (75% of apparent lift) | +1,123 |
| F: +cross-tf+mom | +1,794 | -1,416 | -3,210 | +1,687 |
| E: combined | +2,634 | -192 | -2,826 | +2,912 |

Key observations (corrected):
- **Momentum is the strongest single family** (+2,032 bps lift vs V1). Consistent across hourly and 4h regimes.
- **Cross-TF provides modest lift** (+1,123 bps) but ~75% of its previously apparent signal was lookahead.
- **Combined (E) is best overall** (+2,912 bps lift), driven primarily by momentum and other families, not cross-TF.
- **F (cross-TF + momentum) underperforms A (momentum alone)**: +1,687 vs +2,032. Cross-TF may be adding noise that hurts momentum.
- All candidates remain unprofitable in absolute terms on this short window.
- **Caution**: 4 folds on 693 rows is still a small evaluation window.

### 4h robustness study — separate experiment (2026-03-28)

**Purpose**: Test whether feature lift persists in a coarser, longer-window regime. This is NOT a replacement for the hourly benchmark — it is a separate robustness check.

**Dataset**: 721-row DOGE/USD 4h OHLCV (manifest hash from `data/research-4h/`). Date range: 2025-11-29 to 2026-03-29 (120 days).

**Important differences from hourly benchmark**:
- Base resolution: 4h bars (not 1h)
- "6h" label = `close.shift(-6)` = 6 bars ahead = **24h forward return** (not 6h)
- Feature rolling windows cover 4x more clock time
- Cross-TF features produce 16h/48h aggregation (not 4h/12h)
- Cross-TF multi-resolution concept is preserved (base → longer blocks), but at coarser scales

**Config**: 7-fold walk-forward, 90d train / 1d val / 5d step, fee 10 bps, slippage 5 bps

Results (primary candidates):

| Candidate | Trades | Accuracy | Net P&L (bps) | Sharpe | Brier | MaxDD |
|-----------|--------|----------|---------------|--------|-------|-------|
| V1 control | 32 | 28.1% | -2,623 | -19.7 | 0.320 | 5,113 |
| A: +momentum | 30 | 33.3% | +95 | +0.7 | 0.311 | 2,906 |
| B: +vol/regime | 19 | 42.1% | -138 | -1.6 | 0.308 | 1,931 |
| D: +cross-tf | 32 | 28.1% | -2,862 | -20.4 | 0.311 | 4,261 |
| F: +cross-tf + momentum | 31 | 29.0% | -2,050 | -14.5 | 0.308 | 3,689 |
| E: combined (all 4) | 29 | 44.8% | -1,240 | -9.2 | 0.338 | 2,899 |

Key findings:
- **Cross-TF signal does NOT transfer to 4h**: D adds nothing over V1 (identical accuracy, worse P&L). The 4h/12h aggregation that worked on hourly data collapses when the base is already 4h.
- **Momentum is the most robust feature family**: A is the only positive-P&L candidate (+95 bps). It transfers from hourly to 4h where cross-TF does not.
- **Vol/regime improves at 4h**: B goes from weakest family on hourly to second-best on 4h (42.1% accuracy, near-zero P&L).
- **E (combined) gets its 4h lift from momentum + vol/regime, not cross-TF**.
- **F (cross-TF + momentum) underperforms A (momentum alone)** at 4h — cross-TF features are noise at this resolution.
- **All candidates are unprofitable** except A (barely positive). This is a harder regime than the short hourly window.

Implications for promotion:
- Cross-TF signal appears resolution-specific (strong at 1h, absent at 4h)
- Momentum signal is more robust across resolutions
- No candidate is promotion-worthy from this study alone
- The hourly cross-TF lift should be treated with caution — it may be fragile

### CryptoCompare-backed 180d hourly benchmark — separate track (2026-03-29)

**Purpose**: Longer hourly evaluation not possible with Kraken REST OHLC (720-candle limit). CryptoCompare `histohour` with `e=Kraken` provides 180 days of Kraken-specific hourly candles.

**Dataset**: 4,320-row DOGE/USD 1h OHLCV (`data/research-cc-180d/`). Source: `cryptocompare`, exchange: `Kraken`. Date range: 2025-09-30 to 2026-03-29 (180 days).

**30-day overlap validation (PASSED)**: Against Kraken-native 693-row dataset — close max diff 0.05%, volume mean diff 0.004%, timestamps 693/693 aligned perfectly. Source effect on V1 P&L: -303 bps (~10% of 30d loss), not the driver of longer-window improvement.

**Config**: 18-fold walk-forward, 90d train / 1d val / 5d step, fee 10 bps, slippage 5 bps

Feature selection results (180d, V1 vs feature families):

| Candidate | Folds | Trades | Accuracy | Net P&L (bps) | Sharpe |
|-----------|-------|--------|----------|---------------|--------|
| V1 (7 features) | 18 | 214 | 47.2% | +5,531 | +11.3 |
| A: +momentum (11) | 18 | 235 | 44.3% | -52 | -0.1 |
| E: combined (22) | 18 | 292 | 43.8% | -3,291 | -4.5 |

**V1 wins decisively on the longer window.** Feature engineering hurts — momentum and combined overfit on short training windows but fail with 90d of data. Simpler features resist overfitting.

### V1 probability calibration (180d CC-backed track, 2026-03-29)

**Protocol**: Same V1 7-feature pipeline, same walk-forward config. Calibration via `CalibratedClassifierCV` with inner 3-fold CV on training data only — calibrator never sees the outer validation fold. Leakage-safe by construction.

| Candidate | Trades | Accuracy | P&L (bps) | Sharpe | Brier | LogLoss | Hit Rate | MaxDD |
|-----------|--------|----------|-----------|--------|-------|---------|----------|-------|
| V1 uncalibrated | 214 | 47.2% | +5,531 | +11.3 | 0.2551 | 0.7033 | 53.7% | 3,738 |
| V1 + Platt | 171 | 43.3% | -2,326 | -6.6 | 0.2566 | 0.7064 | 47.4% | 4,633 |
| **V1 + isotonic** | **243** | **50.2%** | **+11,479** | **+20.2** | **0.2548** | **0.7030** | **56.4%** | **2,902** |

Delta vs V1 uncalibrated:

| | dP&L | dSharpe | dBrier | dLogLoss | dTrades | dHitRate |
|---|---|---|---|---|---|---|
| V1 + Platt | -7,857 | -17.8 | +0.0015 | +0.0031 | -43 | -6.4pp |
| **V1 + isotonic** | **+5,948** | **+9.0** | **-0.0003** | **-0.0003** | **+29** | **+2.6pp** |

**Decision**:
- **V1 + Platt: REJECT.** Destroys trading performance (-7,857 bps). Platt scaling makes predictions more conservative in the wrong direction — abstains on profitable trades.
- **V1 + isotonic: RECOMMENDED.** P&L nearly doubles (+5,948 bps), Sharpe +9.0, hit rate +2.6pp, lower drawdown (-836 bps). Probability quality maintained (Brier/LogLoss essentially unchanged). More trades (243 vs 214) and the additional trades are net-profitable.

**Calibration verdict (2026-03-29)**:

Non-TS-safe isotonic (random K-fold inner CV) showed +11,479 bps — but this was inflated by temporal leakage in the calibration split.

Time-series-safe isotonic (tail holdout 80/20, `cv="prefit"`):

| Candidate | Trades | Accuracy | P&L (bps) | Sharpe | Brier | Hit Rate | Fold W/L/T |
|-----------|--------|----------|-----------|--------|-------|----------|------------|
| V1 uncalibrated | 214 | 47.2% | +5,531 | +11.3 | 0.255 | 53.7% | — |
| V1+isotonic (TS-safe) | 292 | 46.9% | +1,565 | +2.1 | 0.267 | 51.0% | 5/12/1 |

**Decision: V1 uncalibrated is the research winner.** Isotonic calibration degrades P&L by -3,967 bps and Sharpe by -9.2 under time-series-safe conditions. It loses 12 of 18 folds. The non-TS-safe result was a calibration artifact — isotonic overfit when allowed to see "future" data within the training window via random K-fold. Platt scaling was already rejected (destroys trading performance).

### What the bot can do now

- **Bearish DOGE/USD**: sells DOGE inventory (spot transition, no Position created)
- **Bullish DOGE/USD**: buys DOGE with free USD (creates Position with stop/target)
- **Fills**: tracks via structured PendingOrder, partial fill support
- **Risk**: DOGE is managed long exposure in concentration numerators
- **Reconciliation**: syncs balances from exchange, prunes stale pending orders
- **Beliefs**: technical_ensemble polls OHLCV hourly, 6-signal TA → consensus
- **Dashboard**: live at localhost:58392 with SSE updates

### Running the bot

```powershell
# Smoke test (safe mode, exits after reconcile)
python main.py

# Writable run (trades when signals are directional)
$env:STARTUP_RECONCILE_ONLY='false'
$env:READ_ONLY_EXCHANGE='false'
$env:DISABLE_ORDER_MUTATIONS='false'
$env:ALLOWED_PAIRS='DOGE/USD'
$env:MAX_POSITION_USD='10'
$env:MIN_POSITION_USD='10'
$env:WEB_PORT='58392'
python main.py
```

## Completed tasks

- **Task 1** ✅ `.env.example`, `main.py` entry point, safe mode flags
- **Task 2A** ✅ Authenticated read-only Kraken REST
- **Task 2B** ✅ Kraken mutation execution (execute_order, execute_cancel)
- **Task 3** ✅ Local-first migration (SQLite adapter, config flipped)
- **Task 3B** ✅ SQLite write support (SqliteWriter: positions, orders, ledger)
- **WebSocket** ✅ Kraken WS v2 (connection manager, ticker, executions, fallback)
- **Runtime loop** ✅ Wired WebSocket into scheduler, dashboard, heartbeat
- **Pair whitelist** ✅ ALLOWED_PAIRS config + OrderGate enforcement
- **Executor wiring** ✅ Safe mode flags flow from Settings to KrakenExecutor
- **Smoke + read-only + writable runs** ✅ All verified
- **Reducer** ✅ 7 event handlers, belief consensus entry, stop/target exit, fill tracking, risk gating
- **Runtime integration** ✅ PlaceOrder/CancelOrder/ClosePosition to executor, WS fills → reducer
- **Belief pipeline** ✅ Technical ensemble (6-signal TA) + OHLCV fetch, periodic polling
- **Spot inventory** ✅ Bearish DOGE sells, structured PendingOrder, derived reservation, buy gated by USD
- **Portfolio** ✅ DOGE-inclusive total_value_usd, mark_to_market(), DOGE as managed exposure in risk
- **Dashboard** ✅ HTML served at /, SSE real-time updates
- **TUI Cockpit** ✅ Read-only operator cockpit (Textual + Rich, 7 screens, SSE live, 54 tests)
- **Phase 0** ✅ Renamed autoresearch → technical_ensemble
- **Research specs** ✅ Codex-authored integration spec + implementation checklist
- **Phase 1** ✅ Research dataset export (OHLCV history, DB reader, labels, builder, CLI, 34 tests)

## Session Commits (2026-03-26, Phase 5a evaluation)

### kraken-bot-v4
```
a584999 fix: deduplicate OHLCV timestamps across pagination boundaries
```

### autoresearch
```
4dbb288 build(phase-5a): complete LLM evaluation — logistic regression wins
2697633 build(phase-5a): upgrade default LLM to qwen3.5:9b
b732d54 build(phase-5a): add LLM candidate via Ollama with 6h decision cadence
```

### Previous sessions
```
# autoresearch (Phase 2-4, 2026-03-25)
6398c0e build(phase-4): add artifact schema, promotion workflow, and CLI commands
6aa2636..f9ce514 build(phase-2/3): walk-forward harness, baselines, storage, CLI

# kraken-bot-v4 (Phase 2-4 + TUI, 2026-03-25/26)
94a10ce docs: update continuation prompt after Phases 2-4 completion
3682eb0 docs(phase-4): add artifact contract v1 for research model integration
```

## Current State

- **kraken-bot-v4 branch**: master, at `a584999`
- **autoresearch branch**: master, at `4dbb288`
- **kraken-bot-v4 tests**: 447 passed (393 existing + 54 TUI), ruff clean
- **autoresearch tests**: 119 passed, 11 skipped (parity tests, Python 3.10 vs 3.11)
- **Trading bot**: unchanged, still live-capable with TA ensemble beliefs
- **Evaluation harness**: fully operational with 8 ablation candidates + 4 original candidates; all results persisted as JSON experiment records
- **Kraken-native 30d benchmark**: V1 -3,103 bps (4 folds, 10d train). No profitable candidate on short window.
- **CC-backed 180d benchmark**: V1 **+5,531 bps, Sharpe 11.3** (18 folds, 90d train, source: CryptoCompare e=Kraken). Features overfit — A (-52 bps), E (-3,291 bps) both lose to V1.
- **30d overlap validation**: PASSED (close <0.05% diff, timestamps 693/693, source effect ~300 bps)
- **Hourly ablation (corrected 2026-03-29)**: Cross-TF lookahead bug fixed. Momentum strongest single family on short window; V1 wins on long window.
- **4h robustness study (separate)**: Momentum only profitable family. Cross-TF does not transfer.
- **Cross-TF deprioritized**: 75% of apparent lift was lookahead artifact.
- **LLM path**: proven infrastructure, does not beat LogReg, Phase 5b paused
- **Benchmark parity**: resolved — dataset dedup (commit a584999) was the sole cause of the mismatch
- **Artifact contract**: defined and synced to kraken-bot-v4
- **IPEX-Ollama**: working at `C:\Users\rober\ipex-ollama\` with Intel Arc GPU acceleration

## Key Paths

| File | Purpose |
|------|---------|
| `SPEC.md` | Full system spec (local-first architecture) |
| `docs/specs/autoresearch-trading-research-spec.md` | Offline research integration spec |
| `docs/specs/autoresearch-trading-implementation-checklist.md` | Phased implementation plan |
| `main.py` | Entry point — wires settings, executor, belief handler into runtime |
| `runtime_loop.py` | WebSocket, dashboard, heartbeat, effect dispatch, fill bridging, belief poll |
| `core/config.py` | Env-driven config (includes ALLOWED_PAIRS) |
| `core/types.py` | BotState, PendingOrder, FillConfirmed (with client_order_id) |
| `core/state_machine.py` | Reducer — spot sell, buy entry, fills, reconciliation, risk |
| `scheduler.py` | Orchestrator — pending_fills/beliefs, reference_prices injection, mark_to_market |
| `beliefs/technical_ensemble_source.py` | 6-signal TA ensemble (was autoresearch_source) |
| `beliefs/technical_ensemble_handler.py` | OHLCV fetch + TA → BeliefSnapshot |
| `exchange/ohlcv.py` | Kraken public OHLCV fetch |
| `exchange/order_gate.py` | Single mutation gate with pair whitelist |
| `exchange/executor.py` | Kraken executor (read + write, safe mode enforcement) |
| `exchange/websocket.py` | Kraken WS v2 (ticker, executions, fallback) |
| `trading/portfolio.py` | Portfolio accounting, DOGE-inclusive, mark_to_market() |
| `trading/risk_rules.py` | Risk checks with DOGE as managed long exposure |
| `trading/position.py` | Position lifecycle (open/close/update stop/target) |
| `trading/sizing.py` | Kelly criterion + bounded sizing |
| `persistence/sqlite.py` | SQLite adapter (WAL, reader + writer) |
| `research/ohlcv_history.py` | Paginated Kraken OHLCV fetch with timestamps |
| `research/db_reader.py` | SQLite reader for fills/orders/closed trades (research) |
| `research/labels.py` | Forward-looking labels (return_sign/bps, regime) |
| `research/dataset_builder.py` | Orchestrates Parquet + manifest export |
| `research/cli.py` | CLI: python -m research.cli |
| `build/manifests/phase-10.research-dataset.json` | Phase 10 task manifest |
| `web/app.py` | FastAPI + SSE + static file serving |
| `tui/app.py` | TUI operator cockpit (Textual App, key bindings, screen switching) |
| `tui/client.py` | Dashboard HTTP client (snapshot fetch) |
| `tui/events.py` | SSE stream reader (async generator) |
| `tui/state.py` | Presentation state + JSON parsers |
| `tui/screens/` | 7 screens: overview, positions, beliefs, orders, reconciliation, logs, help |
| `tui/widgets/` | Reusable widgets: health, portfolio, positions, beliefs, orders, reconciliation, event_log, status_bar |
| `docs/specs/tui-operator-cockpit-spec.md` | TUI v1 spec |

## Environment

- **Platform**: Windows 11, Git Bash
- **Python**: 3.13
- **Deployment**: Spare laptop (bot + SQLite + dashboard), Tailscale for remote
- **Subscriptions**: Claude Max + Codex Max ($0 marginal LLM cost)
- **Repo**: git@github.com:robjohncolson/kraken-bot-v4.git
- **Known issue**: School network may filter Kraken API — use hotspot for live tests
