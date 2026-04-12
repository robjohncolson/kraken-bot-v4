# Continuation Prompt — kraken-bot-v4

## Architecture

```
CC Brain (scripts/cc_brain.py) — runs every 2h or on-demand
  ├── Reads temporal memory (what happened last time)
  ├── Observes portfolio (balances, positions)
  ├── Analyzes pairs:
  │     ├── HMM regime detection (trending/ranging/volatile)
  │     ├── RSI(14) + EMA(7/26) on 1H and 4H bars
  │     └── Kronos (24h full-OHLCV candle prediction, Intel Arc GPU)
  ├── Scores entries (gates: regime > 0.40, 4H up, RSI oversold)
  ├── Places orders or sits out
  ├── Writes memories (decisions, regime, snapshots)
  └── Generates review report

Bot (always-on, deterministic body)
  ├── WebSocket price streaming
  ├── TP/SL/trailing stop monitoring
  ├── Fill settlement + reconciliation
  └── REST API for CC to read/write
```

- **Control split**: Bot = dumb body. CC = brain. `CC_BRAIN_MODE=true` disables bot's autonomous planner.
- **Host**: spare laptop at home, always-on
- **Exchange**: Kraken (Starter tier) — source of truth for balances/orders/fills
- **Persistence**: SQLite (`./data/bot.db`, WAL mode) — positions, orders, ledger, cooldowns, rotation tree, pair_metadata, cc_memory
- **Dashboard**: FastAPI + D3.js at `http://0.0.0.0:58392` (LAN-accessible)
- **TUI**: `python -m tui` — Textual/Rich operator cockpit, 8 screens (key 7 = Rotation Tree)
- **Platform**: Windows 11, Python 3.13, Intel Arc GPU (torch 2.8.0+xpu)
- **Repo**: `git@github.com:robjohncolson/kraken-bot-v4.git`, branch `master`

## Current state (as of 2026-04-12, session 3)

**CC IS THE BRAIN** — Bot runs as deterministic body, CC makes all trading decisions.

| Field | Value |
|-------|-------|
| CC Brain Mode | `CC_BRAIN_MODE=true` — bot's planner + root evaluator disabled |
| Belief model | `timesfm` (still wired but CC uses its own signals) |
| CC Signals | RSI(14) + EMA(7/26) + Kronos + TimesFM + HMM regime |
| Portfolio | ~$471 total value (down from $482 start-of-session) |
| 7-day P&L | **−$14.58** (14 trades, 6W/8L, but dominated by one −$15.85 USDT/USD outlier) |
| Scheduled task | Windows Task Scheduler `KrakenBot-CcBrainCycle` fires every 2h, logs to `state/scheduled-logs/` |
| Shadow mode | Live. Writes `shadow_verdict` memories each cycle via `compute_unified_holds` |
| Shadow veto | **Narrow USD-only veto LIVE** (commit b177900) — blocks cash-to-crypto entries when shadow's best hold is USD |
| Shadow analyzer | `scripts/analyze_shadow.py` reads verdicts for forward eval |
| Historical backfill | `scripts/backfill_shadow.py` — has a known bug (counts dry-runs + failed orders as filled) |
| Tests | 679+ passing (TUI suite 54/54, runtime loop unchanged) |

### Session 3 architectural changes (all LIVE on master)

1. **Currency-agnostic stability** (commit `06fb1b1`)
   - `get_asset_volumes()` now credits both base AND quote assets for each pair
   - `compute_stability` log range recalibrated (4.3 → 3.8) so top Kraken asset saturates at 1.0
   - USD gets `vol_pct=0` (principled — no self-pair means no volatility in its own terms)
2. **Shadow-mode unified hold scoring** (commit `06fb1b1`)
   - New `invert_analysis()` flips directional signals for the quote side of any pair
   - New `compute_unified_holds()` aggregates per-asset hold scores via bidirectional pair analysis
   - Requires `n >= 3` for eligibility, uses top-3 mean as aggregator
3. **Shadow verdict persistence + analyzer + promotion doc** (commit `8344ba5`)
   - Each cycle writes a `shadow_verdict` memory with full state
   - `scripts/analyze_shadow.py` reads back and computes agreement stats
   - `tasks/shadow_promotion_criterion.md` spells out the three-part promotion bar
4. **Stablecoin vol_pct fix** (commit `8344ba5`)
   - vol_pct now scales with HMM `probabilities.volatile`, not a fixed ranging default
   - USDT/USDC stability went from ~0.64 → **0.86** with no other changes
5. **TUI Shadow column** (commit `5b3eb4d`) — new holdings column shows per-held top3_mean
6. **Duplicate-entry fix + narrow shadow veto** (commit `b177900`)
   - `get_pairs_with_pending_orders()` reads pending_order memory and blocklists re-proposal
   - **Narrow USD veto is LIVE**: when shadow's best hold is USD, cash-to-crypto entries are blocked
   - Only fires on `best_shadow == "USD"`; does not veto rotations or exits
7. **pair_decimals fix** (commit `5c59540`) — `_price_decimals` now uses Kraken's authoritative `pair_decimals` field (fallback to magnitude heuristic)
8. **Windows Task Scheduler** (commit `42d76e1`) — `scripts/register_scheduled_task.ps1` + `scripts/run_cc_brain_scheduled.ps1`, registered task name `KrakenBot-CcBrainCycle`
9. **Floor-round sell qty** (merged from codex/01-floor-round, `446ba44`)
   - New `_floor_qty(qty, pair)` uses `lot_decimals` from AssetPairs, rounds DOWN
   - Applied to sweep_dust, rotation sells, and exit sells
   - Fixes `EOrder:Insufficient funds` loop that was stuck on CRV/COMP
10. **Backfill forward-return 6h analysis** (merged from codex/05, `f18172e`)
    - Result at `tasks/specs/05-backfill-6h-analysis.result.md`
    - **IMPORTANT**: the analysis script has a methodology bug — it treats dry-runs and failed orders as filled. Corrected ground-truth analysis on the 6 actually-filled cycles shows shadow wins **+11.59%** over 6h (see investigation findings below).

### Session 3 investigation findings (fee + backfill validity)

1. **RAVE +35.25% "live win" never happened** — 2026-04-12 01:32 UTC brain report shows `FAILED: RAVE/USD — EGeneral:Invalid arguments:volume minimum not met`. Kraken rejected the order on ordermin. Codex-05 backfill counted it in live's column; correcting that reverses the cumulative-edge sign.
2. **Corrected shadow analysis (filled cycles only)**: 6/6 shadow wins at 6h, shadow edge **+11.59%**. Shadow was right every time the bot actually committed capital.
3. **Fees are 0.40% roundtrip = 0.20% per side** — that's taker, not maker. Root cause: `scripts/cc_brain.py` lines 1010/1236/1261/1278 set limit price to `price * 1.002` for buys / `price * 0.998` for sells, which crosses the spread and executes as taker. Should be passive maker (or post-only flag).
4. **True underlying performance (ex-USDT outlier) is essentially break-even** — the bot's edge per trade is roughly equal to its fee burden. The entire 7-day `-$14.58` loss is dominated by one suspicious `USDT/USD -$15.85` trade (cost $36.96, proc $21.11 — 43% loss on a stablecoin, almost certainly an accounting error).
5. **Self-tune rule is backwards**: it bumps `MAX_POSITION_PCT` when `fees/gross_wins > 60%`, but bigger positions don't change the fee/win ratio. The correct lever is fee rate per side (maker vs taker).

### Hardening batch status (dispatched via `../Agent/runner/parallel-codex-runner.py`)

Specs + plans live in `tasks/specs/`. Part 1 manifest: `dispatch/kraken-bot-hardening.manifest.json`. Part 2 manifest: `dispatch/kraken-bot-hardening-part2.manifest.json`.

**Part 1 completed 2026-04-12 ~16:00 UTC.** Results:
- 02-open-orders: FAILED — orphaned worktree dir from attempt 1. Deferred to part 2.
- 06-backfill-fidelity: **MERGED** (`b9b08a4`) — filter now correctly drops dry-runs + failed orders from comparisons.
- 09-usdt-investigation: **MERGED** (`7ee738a`) — root cause identified (class A accounting bug in runtime_loop.py). NO code fix applied because the actual bug is in `runtime_loop.py` which wasn't in owned paths. Result file at `tasks/specs/09-usdt-loss-investigation.result.md`.
- 03/07/08/10: blocked by 02 failure; deferred to part 2.

**Part 2 COMPLETED 2026-04-12 ~16:42 UTC.** All 5 remaining agents merged to master via the runner's cc-merge branch. Master HEAD advanced to `28d0650` (+5 merge commits from the runner). Bot restarted to pick up the new `/api/open-orders` route. **12/12 verification checks pass** via `scripts/verify_hardening_batch.py`.

| Spec | Status | Commit |
|------|--------|--------|
| 01 floor-round-exit-qty | **LIVE** | `446ba44` |
| 02 open-orders-tracking | **LIVE** | `757c662` (+ agent `ea6c8b1`) |
| 03 fiat-filter-check-exits | **LIVE** | `415a5e7` (+ agent `9bbb4af`) |
| 04 extended-shadow-veto | **DROPPED** | — |
| 05 backfill-6h-analysis | **MERGED** (stale-result) | `f18172e` |
| 06 backfill-fidelity | **LIVE** | `ec0c5fa` (+ agent `b9b08a4`) |
| 07 ordermin-precheck | **LIVE** | `642455b` (+ agent `8ed523b`) |
| 08 maker-fee | **LIVE** | `0d4603a` (+ agent `010fe7d`) |
| 09 usdt-loss-investigation | **DIAGNOSIS ONLY** (fix in spec 11) | `43c0d8a` (+ agent `7ee738a`) |
| 10 self-tune-rule-fix | **LIVE** | `28d0650` (+ agent `00ffef4`) |

### What actually changed in the bot (net effect)

1. **Fee reduction**: limit-order buffer is now 10 bps (0.10%) instead of 20 bps (0.20%). On liquid pairs this should land as maker fills (0.16% per side) instead of taker (0.25%). Expected improvement: ~20% reduction in per-trade fee rate.
2. **Ordermin/costmin blocking**: entries and rotations that would be rejected by Kraken for minimum-size violations are now filtered at the scoring step. Eliminates the RAVE-class wasted cycles.
3. **Floor-round sell qty**: CRV/COMP/etc will no longer hit EOrder:Insufficient funds on off-by-epsilon rounding.
4. **Open-orders visibility**: `/api/open-orders` returns live exchange state. `cc_brain` unions it with the memory-based pending blocklist. Stale orders get cancelled authoritatively.
5. **Fiat filter**: AUD/CAD/EUR/GBP/CHF/JPY etc. are no longer proposed for exit. The Massachusetts-regulated `EAccount:Invalid permissions:AUD/USD` loop is broken.
6. **Self-tune fix**: the backwards MAX_POSITION_PCT rule is replaced with a correct ENTRY_THRESHOLD tightener. Position size stays where it is.
7. **Backfill fidelity**: `scripts/backfill_shadow.py` now correctly filters out dry-run cycles and failed orders. Re-running with the fix should materially change the cumulative-edge numbers from the previous run.
8. **USDT phantom loss diagnosed**: no code fix yet; follow-up spec 11 targets the actual bug in `runtime_loop.py`. Until that lands, the `trade_outcomes.id=1` row stays as a $15.85 phantom that the self-tune ignores (now that rule 3 is fixed anyway).

**Key insight from 09 diagnosis** (impacts everything else):
The "−$14.58 / 7 days" loss is largely a **phantom** caused by a `runtime_loop.py` bug that stored USDT base quantity in the `exit_proceeds` column instead of USD proceeds. The row `trade_outcomes.id=1` computed `net_pnl = 21.11138898 − 36.9612 = −$15.85` by subtracting unlike units (USDT base qty − USD cost). The actual fills were at parity ($0.99965). **Real underlying P&L is approximately +$1.27, not −$14.58.** This dramatically changes the picture: the bot's signals aren't losing money, a reporting bug is.

**Pre-built post-batch validation:** `scripts/verify_hardening_batch.py` runs smoke-tests for every spec's acceptance criteria. `scripts/hardening_retry_helper.sh` inspects worktree/branch state for recovery sequences.

**Known runner quirks observed this session:**
1. Orphaned worktree dirs from failed runs block retries. Manual `rm -rf state/parallel-worktrees/<agent>` required.
2. Codex's sandbox can't write `.git/worktrees/<name>/index.lock` for its own commit attempts. The runner itself commits successfully from outside the sandbox. No action required.
3. The runner's merge pass is gated on full batch success. Partial successes (06, 09) were not auto-merged; manual merge required.

### Follow-up specs (NOT in current batches)

- **11 — runtime_loop root-exit unit mixing fix**: the actual code fix for the USDT phantom loss. Targets `runtime_loop.py:_settle_rotation_fills()` and `_handle_root_expiry()`. Writes `exit_proceeds` in USD consistently.
- **12 — permissions-aware pair blacklist**: when Kraken returns `EAccount:Invalid permissions` (e.g. AUD/USD trading restricted for US:MA), cache the pair as untradeable in memory. Observed on every recent scheduled cycle for AUD/USD.
- **13 — stale worktree cleanup in parallel-runner**: upstream fix in the Agent repo for the orphaned-worktree quirk.

### CC REST Toolkit

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/balances` | GET | Cash + portfolio value |
| `/api/rotation-tree` | GET | All positions + P&L |
| `/api/trade-outcomes?lookback_days=N` | GET | Trade history for post-mortems |
| `/api/ohlcv/{pair}?interval=60&count=50` | GET | Raw OHLCV bars |
| `/api/kronos/{pair}?interval=60&pred_len=24` | GET | Kronos 24h candle prediction |
| `/api/regime/{pair}?interval=60&count=300` | GET | HMM regime + trade_gate |
| `/api/memory?category=X&hours=N` | GET | Query CC temporal memory |
| `/api/orders` | POST | Place order (JSON body) |
| `/api/memory` | POST | Write to CC memory |
| `/api/orders/{id}` | DELETE | Cancel order |

### CC Scripts

| Script | Purpose |
|--------|---------|
| `python scripts/cc_brain.py` | Full brain cycle (analyze + decide + act) |
| `python scripts/cc_brain.py --dry-run` | Analysis only, no orders |
| `python scripts/cc_postmortem.py` | Standalone post-mortem analysis |

### CC Brain scoring (tuned 2026-04-11)

| Parameter | Value | Notes |
|-----------|-------|-------|
| `MAX_POSITION_USD` | $20 | ~4% of portfolio, halves fee ratio vs $10 |
| `MIN_REGIME_GATE` | 0.15 | Hard floor — below this, score = 0 |
| `SOFT_REGIME_GATE` | 0.40 | Below this, score capped at 0.5 (visible but no entry) |
| `ENTRY_THRESHOLD` | 0.6 | Must exceed to place an order |
| `MIN_RSI_OVERSOLD` | 35 | RSI < 35 = strong oversold (+0.4 score) |

**Score components**: 4H trend (+0.20 UP / -0.15 DOWN), 1H trend (+0.10), RSI (0.20-0.40), Kronos (0.10-0.30), regime (0.10-0.30). Reports show full breakdown per pair.

### Dynamic pair discovery (added 2026-04-11)

- `discover_pairs()` calls Kraken AssetPairs + Ticker APIs, ranks by 24h USD volume
- Two-pass analysis: regime filter first (fast), full analysis on survivors (max 15)
- Cached 1 hour, `TOP_PAIRS` fallback if API fails
- Minimum volume: $50k/24h

### Dust sweep (added 2026-04-11)

- `find_dust_positions()` identifies orphan roots < $5 not in tracked pairs
- `sweep_dust()` attempts market sell, logs failures, writes "stuck_dust" memory
- Runs as part of Step 6 (Act) in every brain cycle

### Post-mortem findings (2026-04-11)

- **Win rate**: 43% (6W / 8L over 14 trades)
- **Profit factor**: 0.13 (terrible — losses far exceed wins)
- **Fee burden**: 84% of gross wins eaten by fees
- **Root trades**: -$16.19 net (22% WR) — bot was auto-selling in ranging markets
- **Child trades**: +$1.60 net (80% WR) — actual entries work when pairs are trending
- **Key insight**: Bot traded too much in ranging markets. HMM regime filter now prevents this.
- **First CC brain run**: Correctly sat out — all pairs ranging (trade_gate < 0.40)

### Known issues (2026-04-11)

- Dust positions (ASTER, AZTEC, BANANAS31) — brain will attempt to sell; if below ordermin, logged as stuck
- `runtime_dlls/` directory cleanup safe (no longer needed)
- cp1252 encoding warnings in bot log (benign)
- AKT was force-closed by Phase 7 fix on restart

## Prediction models

| Model | Endpoint/Source | What it sees | Speed |
|-------|----------------|-------------|-------|
| RSI + EMA | OHLCV bars | Momentum + trend (1H/4H) | Instant |
| TimesFM | `beliefs/timesfm_source.py` | Close-price trajectory (21d → 24h) | ~6s |
| Kronos | `GET /api/kronos/{pair}` | Full OHLCV candle structure (400 bars → 24h) | ~4s |
| HMM Regime | `GET /api/regime/{pair}` | Market state: trending/ranging/volatile | ~200ms |

Kronos repo at `C:/Users/rober/Downloads/Projects/kronos` (cloned from shiyu-coder/Kronos).
Kronos-mini (4.1M params) on Intel Arc GPU. Tokenizer: NeoQuasar/Kronos-Tokenizer-base.

## CC Temporal Memory

SQLite table `cc_memory` in `data/bot.db`. Categories:
- `decision` — what CC decided and why (action, signals, reasoning)
- `observation` — market insights, patterns noticed
- `portfolio_snapshot` — point-in-time state captures
- `regime` — HMM results per pair over time
- `postmortem` — trade analysis summaries
- `param_change` — strategy parameter adjustments with rationale

Module: `persistence/cc_memory.py`. REST: `GET/POST /api/memory`.

## Running

```bash
# Launch bot (Windows Python, from project root)
C:\Python313\python.exe main.py

# Launch TUI (separate terminal)
C:\Python313\python.exe -m tui

# Run CC brain cycle
C:\Python313\python.exe scripts/cc_brain.py          # live
C:\Python313\python.exe scripts/cc_brain.py --dry-run # analysis only

# Run post-mortem
C:\Python313\python.exe scripts/cc_postmortem.py
```

### Key env vars (.env)

```
KRAKEN_API_KEY=...
KRAKEN_API_SECRET=...
CC_BRAIN_MODE=true
BELIEF_MODEL=timesfm
ENABLE_ROTATION_TREE=true
MTF_4H_GATE_ENABLED=true
MTF_15M_CONFIRM_ENABLED=true
MTF_15M_MAX_DEFERRALS=6
MTF_ALIGNED_BOOST=1.15
MTF_COUNTER_PENALTY=0.3
SCANNER_TIMEOUT_SEC=45
WEB_HOST=0.0.0.0
WEB_PORT=58392
READ_ONLY_EXCHANGE=false
DISABLE_ORDER_MUTATIONS=false
MIN_POSITION_USD=10
MAX_POSITION_USD=50
ROTATION_MIN_CONFIDENCE=0.65
SCANNER_MIN_24H_VOLUME_USD=50000
SCANNER_MAX_SPREAD_PCT=2.0
```

## CC Trading Philosophy

- **Simple systems win.** RSI + EMA + Kronos + HMM regime. No other indicators.
- **1% monthly target.** Anything above is bonus that reduces future risk.
- **Don't chase.** If you missed a move, wait for the next setup.
- **Regime first.** Don't trade in ranging markets (HMM trade_gate < 0.40).
- **4H trend alignment.** Only enter with the 4H trend, never against it.
- **Post-mortem everything.** Every closed trade gets analyzed. Patterns become rules.
- **Memory is continuity.** Write decisions and reasoning. Future CC reads them.

## Goal for next session (session 3 continuation)

**You are resuming mid-task.** The user approved "B, then A" which means:
- B = fee investigation — **DONE** (findings in "Session 3 investigation findings" above)
- A = dispatch the hardening batch with new specs 06-10 added

### Immediate next action

Write specs and plans for 06, 07, 08, 09, 10 in `tasks/specs/`. Then update `dispatch/kraken-bot-hardening.manifest.json` to:
- Re-add `02-open-orders` with expanded `owned_paths` including `exchange/models.py` and `exchange/parsers.py`
- Add agents for 06, 07, 08, 09, 10
- **Remove** 04-extended-shadow-veto from the batch (on hold pending more data)
- Re-dispatch via `python ../Agent/runner/parallel-codex-runner.py --manifest dispatch/kraken-bot-hardening.manifest.json --reset`

### After the batch lands

1. Run `scripts/analyze_shadow.py --hours 24` — check how many shadow verdicts have accumulated since the scheduled task started firing
2. Verify the narrow USD veto fired at least once (shadow_wants_cash == True on any cycle where live would otherwise have entered)
3. Re-run the corrected backfill (after spec 06 ships) to get a clean shadow-vs-live comparison
4. Look at fee reduction after spec 08 — should see per-trade fees drop from 0.40% → ~0.32% roundtrip

### Do NOT do (explicit on-hold list)

- Do not extend the shadow veto (spec 04) — evidence doesn't support it yet
- Do not trust `scripts/backfill_shadow.py` output until spec 06 ships (counts dry-runs as filled)
- Do not raise `MAX_POSITION_PCT` further — self-tune rule is backwards
- Do not touch the scheduled task unless it starts misfiring

## What shipped 2026-04-11 (session 2)

- **Scoring overhaul**: Hard gates → weighted components. 4H trend and regime are now score components, not binary gates. Soft regime cap at 0.5 for weak-but-not-dead regimes. Entry threshold raised to 0.6.
- **Score breakdown**: Every pair now shows component-by-component scoring in brain reports (`=> 0.45 [4H_trend=+0.20 RSI=+0.20 ...]`)
- **Position size**: $10 → $20 per trade to halve fee-to-win ratio
- **Dynamic pair discovery**: `discover_pairs()` fetches all Kraken USD spot pairs, ranks by 24h volume, returns top 25. Two-pass analysis: regime filter first, full analysis on survivors.
- **Dust sweep**: Auto-detects orphan roots < $5, attempts market sell, logs stuck dust to CC memory
- **679 tests** (was 676)

## What shipped 2026-04-11 (session 1, 11 commits)

- **Phase 7**: EXPIRED auto-liquidation (AKT unstuck), stablecoin root P&L fix
- **Phase 8**: 4H trend gate + 15M entry confirmation (MTF analysis)
- **CC Command API**: POST /api/orders, DELETE /api/orders/{id}, GET /api/trade-outcomes, GET /api/ohlcv/{pair}, GET /api/balances (Codex-reviewed, all findings addressed)
- **Kronos integration**: GET /api/kronos/{pair} — candle prediction, 4.1M params on Intel Arc GPU
- **HMM regime detector**: GET /api/regime/{pair} — 3-state (trending/ranging/volatile), CC+Codex council design
- **CC temporal memory**: persistence/cc_memory.py — decisions, observations, regimes, post-mortems, param changes
- **CC post-mortem engine**: scripts/cc_postmortem.py — automated trade analysis
- **CC brain loop**: scripts/cc_brain.py — complete 8-step decision cycle
- **CC_BRAIN_MODE**: config flag disabling bot's autonomous planner (Codex-implemented)
- **First CC trade**: AVAX/USD limit buy (all signals aligned: RSI=15, 4H up, Kronos bullish)

## Validation

```bash
python -m pytest tests/ -x           # 679 tests
python -m ruff check .               # clean (pre-existing failures in beliefs/, research/, scripts/)
curl http://127.0.0.1:58392/api/health
curl http://127.0.0.1:58392/api/balances
curl http://127.0.0.1:58392/api/regime/SOL%2FUSD
python scripts/cc_brain.py --dry-run  # verify brain cycle
```
