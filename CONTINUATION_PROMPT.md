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

Specs + plans live in `tasks/specs/`. Manifest at `dispatch/kraken-bot-hardening.manifest.json`. Dispatch prompts at `../Agent/dispatch/prompts/kraken-bot-hardening/`.

| Spec | Status | Notes |
|------|--------|-------|
| 01 floor-round-exit-qty | **MERGED** (codex/01, commit `446ba44`) | Works — verified against real balances |
| 02 open-orders-tracking | **FAILED** — ownership enforcement rejected it | Codex needed to extend `exchange/models.py` + `exchange/parsers.py` which weren't in owned_paths. Also hit git-worktree permission issue on commit. Work was lost. Needs retry with expanded owned_paths. |
| 03 fiat-filter-check-exits | BLOCKED (depends on 02) | Ready to run once 02 lands |
| 04 extended-shadow-veto | ON HOLD | Evidence is not in for the extended veto. The narrow USD veto is sufficient based on current data. Deferring until more real filled-trade data accumulates. |
| 05 backfill-6h-analysis | **MERGED** (codex/05, commit `f18172e`) | BUT the backfill script itself needs a fix (spec 06) — it conflates dry-runs and failures with filled trades. |
| **06 (new)** fix-backfill-script | QUEUED | `scripts/backfill_shadow.py` should skip `Mode: DRY RUN` cycles and only count `PLACED:` (not `FAILED:`) lines |
| **07 (new)** ordermin-precheck | QUEUED | Before proposing an entry, check pair `ordermin`/`costmin` from AssetPairs and skip if budget can't meet them. Would have prevented the RAVE failure. |
| **08 (new)** maker-fee-optimization | QUEUED — **CRITICAL** | Replace `price * 1.002` aggressive limit with passive post-only limits at best-bid/best-ask OR reduce buffer to 0.01-0.05%. Current 0.40% roundtrip → target 0.32% (maker rate). |
| **09 (new)** usdt-loss-investigation | QUEUED — HIGH | Debug the `USDT/USD -$15.85` loss. Probably partial-fill accounting error. That one trade IS the entire 7-day loss. |
| **10 (new)** self-tune-rule-fix | QUEUED — LOW | Remove the "fees/gross_wins > 60% → bump MAX_POSITION_PCT" rule or rewrite it to adjust fee rate instead of position size. |

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
