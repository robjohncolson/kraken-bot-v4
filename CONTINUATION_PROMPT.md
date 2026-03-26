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

## What to do NOW: Phase 1 — Research Dataset Export

The live bot is running with reducer, belief polling, and spot inventory management. The next milestone is building the research dataset for the autoresearch integration.

### What exists

- `docs/specs/autoresearch-trading-research-spec.md` — full spec for offline research loop
- `docs/specs/autoresearch-trading-implementation-checklist.md` — phased checklist
- Phase 0 complete: renamed `autoresearch_source` → `technical_ensemble_source`

### Phase 1 scope

Build a dataset builder that exports time-indexed samples from:
- Kraken OHLCV history (via `exchange/ohlcv.py:fetch_ohlcv`)
- Local DB orders/fills/closed trades (via `persistence/sqlite.py`)
- Labels: `return_sign_6h`, `return_sign_12h`, `return_bps_6h`, `return_bps_12h`, `regime_label`
- Output: `data/research/market_v1.parquet`, `labels_v1.parquet`, `manifest_v1.json`
- No feature may use future data

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
- **Phase 0** ✅ Renamed autoresearch → technical_ensemble
- **Research specs** ✅ Codex-authored integration spec + implementation checklist

## Session Commits (2026-03-25)

```
689c3c1 build: serve dashboard HTML + add autoresearch integration specs
eca8508 refactor: rename autoresearch to technical_ensemble (Phase 0)
48a8385 build: add spot inventory management for bearish DOGE sells
a516332 build: wire live belief polling into runtime
c4771b6 docs: update continuation prompt after reducer implementation
2ec30ea build: implement reducer-driven runtime event handling
```

Previous session commits:
```
d6ee7da build: add ALLOWED_PAIRS whitelist, fix executor wiring, add runtime loop
d2c1656-60da50b docs + manifests for phases 7-9
66d336f-b3636fd build(phase-9): WebSocket v2
fafad2b-f1b824b build(phase-8): SQLite writes
b9d6ee8-43b86ad build(phase-7): Mutations
```

## Current State

- **Branch**: master, at `689c3c1`
- **Tests**: 359 passed, ruff clean
- **Kraken account**: ~5,522 DOGE, $0.009 USD (dust), 0 open orders
- **Reducer**: LIVE — all 7 event handlers, spot inventory sell, structured PendingOrder
- **Runtime**: Fully wired — effects dispatch, WS fills → reducer, belief polling
- **Beliefs**: technical_ensemble (neutral 0.50 at last poll)
- **Trading**: Will sell DOGE on bearish signal, buy on bullish with free USD
- **Dashboard**: http://127.0.0.1:58392
- **Unstaged**: AGENTS.md, CLAUDE.md (GitNexus section updates)

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
| `web/app.py` | FastAPI + SSE + static file serving |

## Environment

- **Platform**: Windows 11, Git Bash
- **Python**: 3.13
- **Deployment**: Spare laptop (bot + SQLite + dashboard), Tailscale for remote
- **Subscriptions**: Claude Max + Codex Max ($0 marginal LLM cost)
- **Repo**: git@github.com:robjohncolson/kraken-bot-v4.git
- **Known issue**: School network may filter Kraken API — use hotspot for live tests
