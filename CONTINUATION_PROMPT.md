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

## Current state (as of 2026-04-05)

**POST-MORTEM COMPLETE**: The bot suffered a ~26% portfolio decline ($500→$370). Root cause: the execution layer was fundamentally broken — **zero trades ever completed**. 82 orders placed, zero fills recorded, zero P&L realized. The loss was pure holding exposure on declining altcoins. See `tasks/postmortem-recovery-plan.md` for full analysis.

**Phase 1 (execution layer) is now fixed** (commit `65f1635`). Phases 0, 2-4 remain.

| Field | Value |
|-------|-------|
| Belief model | `llm_council` (CC+Codex via tmux-bridge) |
| Portfolio | ~$370 fragmented across 15 roots (needs Phase 0 consolidation) |
| Rotation tree | **LIVE** — `ENABLE_ROTATION_TREE=true`, 15 root nodes |
| Tests | **612 passing** |
| Execution layer | **FIXED** — startup + periodic reconciliation, child rehydration, cancel persistence |
| Price-aware exits | TP=3%, SL=-2% (Phase 2 will fix to TP=5%, SL=2.5%) |
| Dashboard | `http://10.0.0.24:58392` |

### Portfolio (from SQLite, 2026-04-05)

| Status | Nodes | Entry Cost | Assets |
|--------|-------|-----------|--------|
| CLOSING | 9 | $214 | ATOM, BTC, ETH, KSM, LINK, RAVE, SOL, UNITAS, XRP (exit orders unfilled) |
| OPEN | 6 | $125 | USD ($78), EUR ($22), USDT ($17), TON ($17), GBP ($11), USDC ($10) |

**Key problem**: Portfolio still fragmented. Phase 0 (manual triage) needed: enable safe mode, cancel orphaned Kraken orders, consolidate altcoins to USD.

## Belief models

| Model | Env value | How it works |
|-------|-----------|-------------|
| `technical_ensemble` | Default | 6-signal TA (EMA, RSI, MACD, Bollinger, momentum) |
| `research_model` | Requires `ACTIVE_ARTIFACT_ID` | V1 LogReg (+5,531 bps on 180d backtest, all rollout gates pass) |
| `llm_council` | Requires broker sidecar | CC+Codex analyze structured market context via file-based messaging |

### LLM Council (with fallback chain)

- Handler: `beliefs/llm_council_handler.py` — builds market context, writes request files
- **Fallback**: `make_fallback_council_handler()` wraps council + `technical_ensemble`. If no fresh consensus, falls back to TA instantly. Bot is never belief-less.
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
BELIEF_MODEL=llm_council
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
```

## Goal for next session

### Priority 1: Phase 0 — Emergency Triage

Full plan at `tasks/postmortem-recovery-plan.md`. Manual steps:
1. **Enable safe mode**: `READ_ONLY_EXCHANGE=true`, `DISABLE_ORDER_MUTATIONS=true` in `.env`
2. **Cancel orphaned orders**: Run `scripts/triage_cancel_orders.py` (or manual via Kraken web) → cancel all open orders, update DB
3. **Clean rotation tree**: Reset CLOSING roots to OPEN, verify quantities match exchange
4. **Consolidate to USD**: Sell all altcoin positions to establish clean baseline

### Priority 2: Phase 2 — Risk Management

After Phase 0 consolidation:
- Fix TP/SL ratio: TP=5%, SL=2.5% for ~2:1 R:R after fees (`core/config.py:28-29`, `runtime_loop.py:1014-1023`)
- Activate trailing stops: Ratchet stop_loss_price using trailing_stop_high (`runtime_loop.py:1122-1134`)
- Add root-level stop loss: ROOT_STOP_LOSS_PCT=10% emergency exit
- Enable variable sizing: MAX_POSITION_USD=50

### Priority 3: Phase 3+4 — Signal Quality + Observability

- Fix peak window estimation floor (2h→6h minimum)
- LLM council majority vote instead of hard-fail on disagreement
- Trade outcomes table + win/loss tracking
- Child node unrealized P&L display
- Telegram alerts

### Lessons learned (2026-04-05 post-mortem)

- **Execution layer was broken from day one**: No `cancel_order()` DB method existed — 82 orders stuck as `status='open'` forever. Child nodes were never rehydrated on restart. Orders that filled during bot downtime were permanently lost.
- **Nonce safety**: NEVER use a separate script to call authenticated Kraken API while the bot is running.
- **Shadow ledger divergence**: The rotation tree's `quantity_free` can diverge from actual Kraken balances. Periodic reconciliation now catches drift.
- **Portfolio fragmentation**: Rotation tree creates many small positions. Without root exit windows + consolidation, these accumulate and become individually untradeable.
- **TP/SL math was marginal**: 3% TP + 0.52% fees = 3.52% move needed. 2% SL without fee adj = effective R:R of ~1.3:1. Need 2:1 minimum.
- **Entry cost was wrong**: Used planned allocation, not actual fill cost. Now uses `fill_qty * fill_price` with unspent capital refunded to parent.

## What shipped 2026-04-05

- **Post-mortem analysis**: Full investigation of 26% portfolio decline → root cause: zero trades ever completed
- **Execution layer fix (Phase 1)**: `cancel_order()` in SqliteWriter, startup + periodic order reconciliation against Kraken trade history, child node rehydration on restart, `_execute_cancel_order` now resolves client_order_id → exchange txid, REST fallback poller uses exact fill data from trade history, entry cost uses actual fill cost with parent refund
- **KrakenTrade enriched**: `side`, `quantity`, `price` fields added to model + parser
- **Exchange order ID preservation**: Rehydrated pending orders retain their exchange txid for reconciliation
- 68 new tests (612 total), ruff clean
- Recovery plan at `tasks/postmortem-recovery-plan.md` (Phases 0, 2-4 remain)

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
python -m pytest                    # 612 tests
python -m ruff check .              # clean
curl http://127.0.0.1:58392/api/health         # dashboard up
curl http://127.0.0.1:58392/api/rotation-tree  # rotation tree state
```
