# Continuation Prompt — kraken-bot-v4

## Architecture (local-first, decided 2026-03-24)

- **Spare laptop at home**: single always-on runtime host
- **Kraken**: truth for live balances/orders/fills
- **SQLite** (`./data/bot.db`): durable coordination store (WAL mode)
- **Local JSONL**: audit/recovery trail (ledger, snapshots, offline queue)
- **FastAPI dashboard**: local on bot host (`localhost:58392` — port 8080/8081 may collide)
- **Tailscale**: remote access from school
- **No Supabase** in runtime path (legacy code retained, not used)
- **No Railway** (dashboard is local)

## What to do NOW: Wire belief sources to the reducer

The reducer is implemented and the runtime is fully wired end-to-end. The bot will not trade until belief sources feed into the reducer. The next milestone is connecting the belief pipeline so the reducer receives `BeliefUpdate` events and can act on them.

### What the reducer can do (as of `2ec30ea`)

- **BeliefUpdate**: checks consensus (2/3 sources agree), runs risk/cooldown/max-positions gates, sizes position at MIN_POSITION_USD ($10), emits `PlaceOrder`
- **FillConfirmed**: matches fills to pending orders, opens position with stop/target via PositionLifecycle
- **StopTriggered / TargetHit**: closes position via PositionLifecycle + PortfolioManager (with DOGE accumulation on target hit)
- **ReconciliationResult**: risk check → block entries on soft drawdown, close all on hard drawdown
- **PriceTick / GridCycleComplete**: no-op for now (Guardian handles monitoring, grid deferred)

### What's missing for live trading

1. **Belief source wiring**: `beliefs/orchestrator.py` exists but is not called from the runtime. Need to connect it as the `belief_refresh_handler` in `SchedulerRuntime`, or trigger it externally and feed results via `enqueue_belief()`.
2. **Entry price from market data**: The reducer emits `PlaceOrder` with `quantity` but no `limit_price` (set to `None`). The `OrderGate` or executor needs a current market price to set the limit. Options: use last PriceTick from state, or let the executor fill it from the order book.
3. **Stats engine for Kelly sizing**: Currently falls back to `MIN_POSITION_USD` because `kelly_fraction=0` (no trade history). Need `stats/` module to compute win/loss from ledger and feed real Kelly fractions.
4. **Grid activation**: The reducer logs `GridCycleComplete` but doesn't activate grids. Grid engine exists in `grid/engine.py` and is ready to wire when ranging regime is detected.

### Recommended next steps (in order)

1. Wire `beliefs/orchestrator.py` into the runtime's `belief_refresh_handler`
2. Set `limit_price` on entry orders from current market price (PriceTick state)
3. Run a writable test with a manual belief injection to verify the full entry→fill→position cycle
4. Connect stats engine for Kelly sizing
5. Wire grid activation for ranging regime

### Running the bot

```powershell
# Smoke test (safe mode, exits after reconcile)
python main.py

# Writable run (no orders placed without beliefs)
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
- **Executor wiring** ✅ Fixed: safe mode flags now flow from Settings to KrakenExecutor
- **Smoke test** ✅ Reconcile-only against real Kraken
- **Read-only runtime** ✅ Dashboard, WebSocket, heartbeat verified
- **Writable run** ✅ All 6 criteria passed (no orders — no-op reducer at the time)
- **Reducer** ✅ 7 event handlers, belief consensus entry, stop/target exit, fill tracking, risk gating
- **Runtime integration** ✅ PlaceOrder/CancelOrder/ClosePosition wired to executor, WS fills bridged to reducer via pending_fills

## Session Commits (2026-03-25)

```
2ec30ea build: implement reducer-driven runtime event handling
d6ee7da build: add ALLOWED_PAIRS whitelist, fix executor wiring, add runtime loop
```

Previous session commits:
```
d2c1656 docs: update continuation prompt after phases 7-9 complete
60da50b docs: add phase 7-9 build manifests
66d336f-b3636fd build(phase-9): WebSocket v2
fafad2b-f1b824b build(phase-8): SQLite writes
b9d6ee8-43b86ad build(phase-7): Mutations
```

## Current State

- **Branch**: master, at `2ec30ea`
- **Tests**: 347 passed, ruff clean
- **Kraken account**: ~5,522 DOGE, $0.009 USD (dust), 0 open orders
- **Reducer**: LIVE — handles all 7 event types, emits real actions
- **Runtime**: Fully wired — effects dispatch to executor, WS fills reach reducer
- **Trading**: Will not place orders until beliefs are fed (no belief sources connected)
- **Unstaged**: AGENTS.md, CLAUDE.md (GitNexus section updates, not committed)
- **Untracked**: chatGPT_analysis.md, claude_analysis.md, grok_analysis.md

## Key Paths

| File | Purpose |
|------|---------|
| `SPEC.md` | Full system spec (local-first architecture) |
| `main.py` | Entry point — wires settings into executor + runtime |
| `runtime_loop.py` | WebSocket, dashboard, heartbeat, effect dispatch, fill bridging |
| `core/config.py` | Env-driven config (includes ALLOWED_PAIRS) |
| `core/state_machine.py` | **Reducer** — 7 event handlers, belief entry, stop/target exit |
| `core/types.py` | BotState (with as_of, pending_orders, cooldowns, entry_blocked) |
| `scheduler.py` | Orchestrator — pending_fills, pending_beliefs, guardian, reconciliation |
| `exchange/order_gate.py` | Single mutation gate with pair whitelist |
| `exchange/executor.py` | Kraken executor (read + write, safe mode enforcement) |
| `exchange/websocket.py` | Kraken WS v2 (ticker, executions, fallback) |
| `beliefs/consensus.py` | Belief consensus (2/3 rule) |
| `beliefs/orchestrator.py` | Belief pipeline (not yet wired to runtime) |
| `trading/portfolio.py` | Portfolio accounting + DOGE accumulation |
| `trading/position.py` | Position lifecycle (open/close/update stop/target) |
| `trading/risk_rules.py` | Portfolio risk checks (drawdown, concentration, cooldown) |
| `trading/sizing.py` | Kelly criterion + bounded sizing |
| `persistence/sqlite.py` | SQLite adapter (WAL, reader + writer) |
| `trading/reconciler.py` | Kraken vs recorded state reconciliation |
| `web/app.py` | FastAPI + SSE local dashboard |

## Environment

- **Platform**: Windows 11, Git Bash
- **Python**: 3.13
- **Deployment**: Spare laptop (bot + SQLite + dashboard), Tailscale for remote
- **Subscriptions**: Claude Max + Codex Max ($0 marginal LLM cost)
- **Repo**: git@github.com:robjohncolson/kraken-bot-v4.git
- **Known issue**: School network may filter Kraken API — use hotspot for live tests
