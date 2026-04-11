# Continuation Prompt — kraken-bot-v4

## Architecture

- **Host**: spare laptop at home, always-on
- **Exchange**: Kraken (Starter tier) — source of truth for balances/orders/fills
- **Persistence**: SQLite (`./data/bot.db`, WAL mode) — positions, orders, ledger, cooldowns, rotation tree, pair_metadata
- **Dashboard**: FastAPI + D3.js at `http://0.0.0.0:58392` (LAN-accessible)
- **TUI**: `python -m tui` — Textual/Rich operator cockpit, 8 screens (key 7 = Rotation Tree)
- **Beliefs**: 3 available models (select via `BELIEF_MODEL` env var)
- **Platform**: Windows 11, Python 3.13, WSL for runtime
- **Repo**: `git@github.com:robjohncolson/kraken-bot-v4.git`, branch `master`

## Current state (as of 2026-04-10)

**BOT IS LIVE AND TRADING** — TimesFM belief model activated, Intel Arc GPU operational.

| Field | Value |
|-------|-------|
| Belief model | `timesfm` (Google TimesFM 2.5, Intel Arc GPU) |
| Portfolio | Consolidating — 6 roots closed (bearish exits), 5 open (bullish holds), 2 closing |
| Rotation tree | **LIVE** — `ENABLE_ROTATION_TREE=true`, cycling every 30s |
| Tests | **628 passing** |
| Position sizing | `MAX_POSITION_USD=50`, Kelly sizing spec'd at `tasks/specs/win-rate-improvement.md` (Phase 6C) |
| Execution layer | **FIXED** — startup + periodic reconciliation, child rehydration, cancel persistence |
| Risk management | **FIXED** — TP=5%, SL=2.5%, trailing stops (1.5% activation), root SL=10% |
| Signal quality | **FIXED** — peak window floor 6h, council weighted majority, MIN_CONFIDENCE=0.70 (configurable in Phase 6A) |
| Observability | **FIXED** — trade_outcomes with node_depth, opened_at on roots, accurate entry_cost |
| P&L accuracy | **FIXED** (Phase 5) — root entry_cost recalculated at exit, opened_at set on deadline, node_depth column |
| WebSocket | Connected (was falling back to REST, now recovered) |
| Dashboard | Port 58392 — requires bot restart if port was previously held |
| Child trades | **ZERO** — blocked by budget/confidence gates, fix spec'd in Phase 6A |

### Live root evaluations (first cycle, 2026-04-05)

| Root | TA Direction | Confidence | Action |
|------|-------------|-----------|--------|
| ALPHA | bullish | 0.67 | Below MIN_CONFIDENCE (0.70) — no children spawned |
| BOBA | bearish | 1.00 | Will sell on deadline expiry (6h) |
| ADX, AIO, APU, BTR | neutral | 0.33 | Will sell on deadline expiry (6h) |
| EUR, GBP, TON, USDC, USDT | stablecoin/fiat | — | Skipped by root SL (QUOTE_ASSETS) |

### Known issues at startup

- **Port 58392 conflict**: Previous bot instance may hold the port. Kill old PID or ignore (trading works without dashboard)
- **XLTC, XXLM, XXMR**: Kraken X-prefixed internal names — OHLCV lookup fails. These are dust positions, not critical

## Belief models

| Model | Env value | How it works |
|-------|-----------|-------------|
| `technical_ensemble` | Default | 6-signal TA (EMA, RSI, MACD, Bollinger, momentum) |
| `research_model` | Requires `ACTIVE_ARTIFACT_ID` | V1 LogReg (+5,531 bps on 180d backtest, all rollout gates pass) |
| `llm_council` | Requires broker sidecar | CC+Codex analyze structured market context via file-based messaging |
| `timesfm` | **ACTIVE** | Google TimesFM 2.5 (200M params), close-price forecaster, quantile-based confidence, Intel Arc GPU |

### LLM Council (with fallback chain)

- Handler: `beliefs/llm_council_handler.py` — builds market context, writes request files
- **Fallback**: `make_fallback_council_handler()` wraps council + `technical_ensemble`. If no fresh consensus, falls back to TA instantly. Bot is never belief-less.
- **Consensus**: Weighted majority — 2-of-3 bullish = bullish at scaled confidence (avg_conf * 2/3). Perfect splits = neutral/0.0. Unanimous = full avg confidence.
- Broker: `python scripts/llm_council_broker.py` — dispatches to CC+Codex panes, collects votes
- **Broker hardening**: pane health checks, retry on send failure, stale file cleanup
- Protocol: `state/llm-council/{requests,responses,consensus}/*.json`
- Consensus: 2/2 agree = that direction, split = neutral, 1/2 = that agent's direction at coverage-scaled confidence
- Requires CC+Codex running in tmux panes for council beliefs

### Research model artifact

`artifacts/logistic_regression_20260329_3f73bb8a/` — 7-feature V1 LogReg, threshold 0.55, CryptoCompare-backed 180d dataset.

## Recursive rotation tree

**Spec**: `docs/specs/recursive-rotation-tree-spec.md`

**Vision**: Denomination-agnostic recursive trading. Portfolio holdings become root nodes. Each asset scans all Kraken pairs for bear exits / bull entries. Rotations create timed child nodes. Children recurse within parent windows. Confidence-weighted sizing.

**Complete (R1-R5) — LIVE**:
- `core/types.py`: RotationNode, RotationCandidate, RotationTreeState, RotationEvent
- `trading/rotation_tree.py`: Pure helpers, child cap (max_children), score-sorted allocation
- `trading/rotation_planner.py`: RotationTreePlanner with anti-churn (child count, per-child budget)
- `trading/pair_scanner.py`: Generalized pair discovery, scan_rotation_candidates
- `exchange/pair_metadata.py`: Dynamic ordermin from Kraken API, SQLite-cached
- `exchange/ohlcv.py`: OHLCV fetch with 5-minute TTL cache
- `persistence/sqlite.py`: rotation_nodes + pair_metadata tables
- `runtime_loop.py`: Full execution loop with pre-flight balance check, rotation events, dynamic timeouts
- `web/routes.py`: `/api/rotation-tree` endpoint, beliefs with `filtered` field
- `tui/`: Beliefs display (filtered dimmed), rotation events in footer, foreign orders explained

**Architecture**: Rotation tree is a shadow ledger. Orders placed directly via executor. Fill settlement updates tree. Pre-flight verifies exchange balance (2% safety margin) before each order.

**Root exit windows**: Roots now get TA-evaluated deadlines (EMA/RSI/MACD, clamped 2-48h). On expiry, re-evaluate: sell if bearish/neutral, extend if bullish. Quote-currency roots (USD, USDT, etc.) are skipped. Orphaned assets are no longer permanent — they get evaluated and consolidated.

## Key infrastructure

| Feature | Status |
|---------|--------|
| Position persistence | Done — survives restart via SQLite |
| Rotation tree | **LIVE** — scanning, ordering, settling across all Kraken pairs |
| Root exit windows | **LIVE** — TA-evaluated deadlines, re-evaluate on expiry |
| One order per cycle | **LIVE** — confidence-sorted, one entry per 30s cycle |
| Pair cooldown persistence | **LIVE** — SQLite-backed, survives restart |
| Anti-churn | Max 3 children, top-3 by score, per-child budget gate |
| Ordermin enforcement | Dynamic from Kraken API, cached in SQLite |
| OHLCV cache | 5-min TTL dedup across roots |
| Pre-flight balance | 2% safety margin, committed order tracking |
| Rotation events | Structured TP/SL/timeout/fill/root_exit/root_extended events in SSE + TUI |
| Beliefs display | All beliefs shown (filtered dimmed) |
| TUI cancelled pruning | Cancelled nodes hidden from rotation tree view |
| Settings validation | Startup warnings for out-of-range params |
| Conditional tree (v1) | Built, disabled by default |

## Running the bot

```bash
# Launch bot (from WSL)
cd /mnt/c/Users/rober/Downloads/Projects/kraken-bot-v4
/mnt/c/Python313/python.exe main.py

# Launch TUI (separate terminal)
/mnt/c/Python313/python.exe -m tui

# Launch LLM Council broker (MUST use WSL python3, not Windows Python — needs tmux access)
python3 scripts/llm_council_broker.py
```

### Key env vars (.env)

```
KRAKEN_API_KEY=...
KRAKEN_API_SECRET=...
BELIEF_MODEL=timesfm
BELIEF_STALE_HOURS=2
ALLOWED_PAIRS=                    # empty = all pairs (required for rotation tree)
ENABLE_ROTATION_TREE=true
SCANNER_TIMEOUT_SEC=45            # 15s default too short for USD (hundreds of pairs)
WEB_HOST=0.0.0.0
WEB_PORT=58392
READ_ONLY_EXCHANGE=false
DISABLE_ORDER_MUTATIONS=false
MIN_POSITION_USD=10
MAX_POSITION_USD=10
EXIT_LIMIT_OFFSET_PCT=0.1
ROTATION_MAX_CHILDREN_PER_PARENT=3
ROTATION_MIN_CONFIDENCE=0.65      # Phase 6A: was hardcoded 0.70
SCANNER_MIN_24H_VOLUME_USD=50000  # Phase 6B: minimum 24h USD volume
SCANNER_MAX_SPREAD_PCT=2.0        # Phase 6B: max avg HL spread %
KELLY_MIN_SAMPLE_SIZE=10          # Phase 6C: flat sizing until N child trades
```

## What shipped 2026-04-10

- **TimesFM belief source activated**: torch 2.8.0+xpu from PyTorch XPU index, IPEX 2.8.10+xpu, Intel Arc GPU confirmed (`torch.xpu.is_available()=True`)
- **DLL preload in main.py**: System `C:\Python313\Library\bin` has stale Intel 2025.0.4 DLLs; user Library/bin has correct 2025.1.x. Preload block loads correct DLLs before any torch import. Missing `umf.dll` copied to user Library/bin
- **TimesFM proxies fix**: Added `**kwargs` to `TimesFM_2p5_200M_torch.__init__()` in local timesfm source (`C:\Users\rober\Downloads\Projects\timesfm\...`) to absorb newer huggingface_hub kwargs
- **TimesFM kickstart verified**: SOL/USD -> neutral, trending, 5.9s inference, model weights cached (~882 MB)
- **tmux-bridge MCP**: `.mcp.json` added to project root (WSL Ubuntu, node server)

### Known issues (2026-04-10)
- AKT root stuck in EXPIRED (exhausted 3 recovery attempts)
- cp1252 encoding warnings in bot log (benign, Unicode chars)
- CAD, XLTC, XXLM, XXMR: Kraken X-prefixed pair lookup failures (dust positions)
- `runtime_dlls/` directory contains extracted Intel wheel DLLs (cleanup safe, no longer needed)

## Goal for next session

### Priority 1: Implement Phase 6A — Unblock Child Spawning

Spec at `tasks/specs/win-rate-improvement.md`, Codex prompt at `tasks/codex-win-rate-prompt.md`.

**Why**: Zero child trades have occurred. Two blockers:
1. Small root budgets ($14-25) divided by `max_children=3` fall below `MIN_POSITION_USD` ($10)
2. `MIN_CONFIDENCE=0.70` hardcoded — rejects 4/6-signal candidates at 0.67

**Fix**:
- Make `ROTATION_MIN_CONFIDENCE` env-configurable (default 0.65)
- Dynamic `max_children` capped by budget: `min(configured_max, deployable / min_pos)`
- Deploy with `ROTATION_MIN_CONFIDENCE=0.65` in `.env`

### Priority 2: Implement Phase 6B — Volume & Spread Filters

**Why**: Scanner evaluates ALL pairs with no liquidity check. Low-volume garbage pairs produce noisy signals.

**Fix**:
- Add `SCANNER_MIN_24H_VOLUME_USD=50000` and `SCANNER_MAX_SPREAD_PCT=2.0` config
- Compute from existing OHLCV bars (no extra API calls)
- Filter before TA analysis

### Priority 3: Implement Phase 6C — Kelly Sizing Integration

**Why**: `bounded_kelly()` exists in `trading/sizing.py` but isn't wired into the planner.

**Fix**:
- Add `fetch_child_trade_stats()` to SqliteWriter (queries trade_outcomes WHERE node_depth > 0)
- Wire `kelly_cap` into `compute_child_allocations()` via planner
- Sample gate: flat sizing until 10+ child trades, then Kelly kicks in automatically

### Priority 4: Telegram Alerts (Phase 4.4, stretch)

- Configure bot token and chat ID in `.env`
- Alert on: SL hit, TP hit, fill timeout, WS disconnect, drawdown >5%

### Lessons learned (2026-04-05 post-mortem)

- **Execution layer was broken from day one**: No `cancel_order()` DB method existed — 82 orders stuck as `status='open'` forever. Child nodes were never rehydrated on restart. Orders that filled during bot downtime were permanently lost.
- **Nonce safety**: NEVER use a separate script to call authenticated Kraken API while the bot is running.
- **Shadow ledger divergence**: The rotation tree's `quantity_free` can diverge from actual Kraken balances. Periodic reconciliation now catches drift.
- **Portfolio fragmentation**: Rotation tree creates many small positions. Without root exit windows + consolidation, these accumulate and become individually untradeable.
- **TP/SL math was marginal**: 3% TP + 0.52% fees = 3.52% move needed. 2% SL without fee adj = effective R:R of ~1.3:1. Need 2:1 minimum.
- **Entry cost was wrong**: Used planned allocation, not actual fill cost. Now uses `fill_qty * fill_price` with unspent capital refunded to parent.

## What shipped 2026-04-07

- **Phase 5 — P&L accuracy**: Root entry_cost recalculated at exit time (not stale snapshot), `opened_at` set when deadline assigned (hold_hours now populated), `node_depth` column in trade_outcomes (0=root, 1+=child) for filtering. `pending.side` AttributeError fix on startup reconciliation
- **Phase 6 spec**: Win rate improvement spec at `tasks/specs/win-rate-improvement.md`, Codex prompt at `tasks/codex-win-rate-prompt.md`. Three phases: 6A unblocks child spawning (configurable MIN_CONFIDENCE + dynamic max_children), 6B adds volume/spread filters, 6C wires Kelly sizing
- 4 new tests (628 total)

## What shipped 2026-04-05

- **Post-mortem analysis**: Full investigation of portfolio decline → root cause: zero trades ever completed
- **Phase 1 — Execution layer fix**: `cancel_order()` in SqliteWriter, startup + periodic order reconciliation against Kraken trade history, child node rehydration on restart, `_execute_cancel_order` resolves client_order_id → exchange txid, REST fallback poller uses exact fill data from trade history, entry cost uses actual fill cost with parent refund, KrakenTrade enriched with side/quantity/price, exchange_order_id preserved on rehydration
- **Phase 0 — DB triage**: 82 orphaned orders marked cancelled, 9 CLOSING roots reset to OPEN, no Kraken open orders to cancel (already expired)
- **Phase 2 — Risk management**: TP default 3%→5%, SL default 2%→2.5% (~2:1 R:R after fees), SL trigger tightened by taker exit fee, trailing stop ratchets SL after 1.5% activation threshold, root-level stop loss at 10% USD drawdown (skips stablecoins). New config: `ROTATION_TRAILING_STOP_ACTIVATION_PCT`, `ROOT_STOP_LOSS_PCT`
- **Phase 3 — Signal quality**: Peak window floor raised 2h→6h, LLM council uses weighted majority instead of hard-fail on disagreement (2-of-3 bullish = bullish at scaled confidence), `MIN_CONFIDENCE` raised 0.55→0.70 (requires 5/6 TA signals)
- **Phase 4 — Observability**: `trade_outcomes` table with full P&L tracking (populated on exit settlement), child node unrealized P&L now displayed in dashboard snapshot
- Recovery plan at `tasks/postmortem-recovery-plan.md`
- 80 new tests across all phases (624 total), ruff clean

## What shipped 2026-04-04

- **Dashboard rotation tree panel**: Full-width panel with tree table (Asset, Status, Direction badges, Confidence, Deadline, TTL, P&L), summary bar (tree value, open/closed, deployed, realized P&L). DFS-ordered with depth indentation. `updateRotationTree` handler in app.js, initial fetch from `/api/rotation-tree`
- **Dashboard rotation events panel**: Chronological event feed with color-coded type badges (fill=blue, tp=green, sl=red, root_exit=red, root_extended=green). `updateRotationEvents` handler
- **TA direction persistence**: `ta_direction` field on `RotationNode`, persisted in SQLite, exposed in `RotationNodeSnapshot`. Set in `_evaluate_root_deadlines` and `_handle_root_expiry`
- **Expired root recovery**: EXPIRED roots reset to OPEN with `deadline_at=None` for re-evaluation. Max 3 attempts via `recovery_count` field (persisted in SQLite). OHLCV close price stored as `entry_price` fallback so `_close_rotation_node` can place sell order without WebSocket price
- **P&L for CLOSING/EXPIRED roots**: Snapshot builder computes unrealized P&L for CLOSING and EXPIRED roots (previously only OPEN)
- **Restart-safe status restore**: Broadened root metadata merge on startup — restores CLOSING/EXPIRED status, ta_direction, recovery_count, entry_pair, confidence (previously gated only on entry_cost). EXPIRED nodes now included in SQLite save/fetch
- Spec at `tasks/specs/dashboard-rotation-tree.md`

## What shipped 2026-04-03 (late session)

- **P&L persistence**: `fetch_rotation_tree()` now loads all migration columns (`entry_cost`, `fill_price`, `exit_price`, `deadline_at`, etc.). On startup, persisted fields merged onto fresh root nodes so P&L reflects original cost basis, not current price
- **Snapshot price fallback**: `_build_rotation_tree_snapshot()` receives runtime's `_root_usd_prices` cache (REST OHLCV fallback) as base, overlays fresh WebSocket prices. Assets without `{ASSET}/USD` WebSocket subscriptions now show P&L
- **TUI TTL column**: Time-to-deadline column in rotation tree table. Green >2h, yellow <2h, red <30min, EXPIRED. Between Deadline and P&L columns
- 4 new tests (cached price fallback, WebSocket override, entry_cost SQLite round-trip)

## What shipped 2026-04-03 (spec-and-ship)

- **Root exit windows**: Roots get TA-evaluated deadlines (EMA/RSI/MACD, 2-48h). On expiry: re-evaluate → sell if bearish/neutral, extend if bullish. `evaluate_root_ta()` in pair_scanner, `_evaluate_root_deadlines()` + `_handle_root_expiry()` in runtime_loop
- **No currency special-casing**: Removed QUOTE_ASSETS skip — all roots (including USD, EUR, USDT) get TA evaluation and deadlines
- **Root confidence + side**: `evaluate_root_ta()` returns 3-tuple with confidence (signal agreement). Roots display side and confidence in TUI
- **Eastern time deadlines**: TUI shows deadlines in `MM/DD HH:MM ET` format via `zoneinfo`
- **Unrealized P&L on roots**: `entry_cost` set at first evaluation, unrealized P&L computed as `current_value - entry_cost` in snapshot
- **One order per cycle**: PLANNED nodes sorted by confidence desc, early return after first successful placement. Eliminates stale-balance edge case
- **Pair cooldown persistence**: Rotation pair cooldowns written to SQLite `cooldowns` table on set, loaded on startup. Survives restart
- **TUI cancelled node pruning**: Cancelled nodes filtered from DFS traversal in rotation tree widget
- 37 new tests total
- Specs in `tasks/specs/`

## What shipped 2026-04-02/03 (6 commits)

- **Anti-churn**: Max 3 children per parent, top-3 by confidence score, dynamic entry timeout (25% of window, 30-120min), per-child budget gate
- **Scan efficiency**: OHLCV cache (5-min TTL), underfunded leaf skip
- **Pre-flight balance check**: 2% safety margin on order cost + committed tracking, silent cancel (no cooldown) on insufficient funds
- **Ordermin enforcement**: Dynamic from Kraken AssetPairs API, SQLite-cached 24h, enforced in planner + order gate + grid sizing
- **Beliefs display fix**: All beliefs shown in TUI (filtered ones dimmed)
- **Rotation events**: Structured TP/SL/timeout/fill events in SSE + TUI footer
- **Settings validation**: Startup warnings for out-of-range parameters
- **TUI polish**: Foreign orders explained, rotation event in footer
- **Spec-and-ship skill**: `.claude/skills/spec-and-ship/SKILL.md`

## What shipped 2026-04-01 (13 commits)

- LLM Council fallback chain + broker hardening
- Rotation entry resilience (retry budget, rate limit decay, breaker bypass)
- P&L tracking (entry_cost, fill_price, exit_price, exit_proceeds)
- Confidence-weighted trading (MIN_BELIEF_CONFIDENCE=0.5 gate)
- Portfolio total value (rotation tree root pricing in USD)
- TUI polish (orders + health in SSE, P&L column)
- **Price-aware exits**: TP=3%, SL=-2%, fill timeouts, MARKET order support
- Broker: WSL python3 for tmux access

## Validation

```bash
python -m pytest                    # 628 tests
python -m ruff check .              # clean
curl http://127.0.0.1:58392/api/health         # dashboard up
curl http://127.0.0.1:58392/api/rotation-tree  # rotation tree state
```
