# Continuation Prompt — kraken-bot-v4

## Architecture (local-first, decided 2026-03-24)

- **Spare laptop at home**: single always-on runtime host
- **Kraken**: truth for live balances/orders/fills
- **SQLite** (`./data/bot.db`): durable coordination store (WAL mode)
- **Local JSONL**: audit/recovery trail (ledger, snapshots, offline queue)
- **FastAPI dashboard**: local on bot host (`localhost:8080`)
- **Tailscale**: remote access from school
- **No Supabase** in runtime path (legacy code retained, not used)
- **No Railway** (dashboard is local)

## What to do NOW: First controlled run

All implementation tasks are complete. The bot has full Kraken integration (read + write), SQLite persistence (read + write), WebSocket feeds with REST fallback, and the complete scaffold from phases 1-6.

### Completed tasks:

- **Task 1** ✅ `.env.example`, `main.py` entry point, safe mode flags
- **Task 2A** ✅ Authenticated read-only Kraken REST (transport, parsers, executor)
- **Task 2B** ✅ Kraken mutation execution (execute_order, execute_cancel, SafeModeBlockedError, cl_ord_id retry, circuit breaker)
- **Task 3** ✅ Local-first migration (SQLite adapter, config flipped)
- **Task 3B** ✅ SQLite write support (SqliteWriter: positions, orders, ledger; reconciler DB seeding)
- **WebSocket** ✅ Kraken WS v2 (connection manager, ticker feed, execution feed, FallbackPoller REST fallback)

### Priority tasks remaining (in order):

**1. First smoke test with real `.env`**
- Copy `.env.example` to `.env`, fill in `KRAKEN_API_KEY` + `KRAKEN_API_SECRET`
- Run `python main.py` with `STARTUP_RECONCILE_ONLY=true`
- Should: fetch Kraken balances/orders/trades, read empty SQLite, reconcile (all Kraken assets flagged as "untracked"), exit cleanly
- Fix any issues found

**2. Wire WebSocket into scheduler**
- Connect `KrakenWebSocketV2` to the main scheduler loop
- Subscribe to ticker for active pairs
- Subscribe to execution feed with `get_ws_token()`
- Route PriceTick → belief updates, FillConfirmed → reconciler

**3. First controlled run**
- Minimum position sizes, DOGE/USD as first pair
- Verify: startup reconciliation, heartbeat writing, guardian loop, grid activation
- Dashboard on localhost via Tailscale
- Set `STARTUP_RECONCILE_ONLY=false`, `READ_ONLY_EXCHANGE=false`, `DISABLE_ORDER_MUTATIONS=false`

### Key architectural decisions:

- Spare laptop = single runtime authority (no split state)
- Kraken native stop-loss = only market order exception
- All other orders = limit, +/- 0.4% for maker fees
- Beliefs: Claude Code CLI + Codex CLI + auto-research 6-signal ensemble, 2/3 agreement
- Grid: V2 S0/S1a/S1b/S2 states, no per-slot identity, profit redistribution
- Stats: AP Stats parametric first, normality gate, n>=30, fail closed
- Safe mode defaults to all-on (must opt in to live trading)

## Session Commits (2026-03-25) — Phases 7-9 automated build

```
60da50b docs: add phase 7-9 build manifests for mutations, SQLite writes, WebSocket
66d336f build(phase-9/9.4): add FallbackPoller and get_ws_token for REST fallback
4ebf785 build(phase-9/9.3): extract ws_parsers.py, add execution feed subscription
850570d build(phase-9/9.2): add public ticker subscription with PriceTick emission
b3636fd build(phase-9/9.1): KrakenWebSocketV2 connection manager with reconnect
4912dd0 fix: split semicolon-joined dataclass fields in reconciler.py
f1b824b build(phase-8/8.3): wire reconciler to seed DB via SqliteWriter
2c4724c build(phase-8/8.2): add insert_order and insert_ledger_entry to SqliteWriter
fafad2b build(phase-8/8.1): add SqliteWriter with position insert/update methods
43b86ad build(phase-7/7.3): implement execute_cancel with circuit breaker
f8feafe build(phase-7/7.2): implement execute_order with cl_ord_id integration
b9d6ee8 build(phase-7/7.1): add SafeModeBlockedError and mutation parsers
```

## Previous Session Commits (2026-03-24)

```
0a9ab9d docs: update continuation prompt for local-first architecture
4cfe5b4 docs(task-3): complete SPEC.md local-first migration
8ac73c6 build(task-3): flip config and startup to local-first SQLite
143ec50 build(task-3): add SQLite persistence adapter
11ba30c build(task-2a): authenticated read-only Kraken REST client
e58c239 build(task-1): add .env.example, main.py entry point, safe mode flags
```

## Initial Scaffold Commits

```
aa6f466 fix(phase-6): remove unused WatchdogAnalyzer import
c073986 build(phase-6): complete self-healing skeleton with optional Ollama watchdog
2f7c599 build(phase-5): complete observability phase
c2af597 build(phase-4): complete trading phase
1938d91 build(phase-3): complete belief formation phase
bc5e030 build(scaffold): add spec and local build orchestration
```

## Current State

- **Branch**: master, pushed to origin at 60da50b
- **Tests**: 317 passed, ruff clean
- **Architecture**: local-first (SQLite + local dashboard + Tailscale)
- **Startup path**: config → SQLite → Kraken health → Kraken state → recorded state → reconcile → exit or loop
- **Safe mode**: all flags default to true
- **Build phases**: 1-9 complete (1-6 scaffold, 7 mutations, 8 SQLite writes, 9 WebSocket)
- **Untracked**: chatGPT_analysis.md, claude_analysis.md, grok_analysis.md, doge-bot-env.txt, grid-bot-v2-envs.txt

## Key Paths

| File | Purpose |
|------|---------|
| `SPEC.md` | Full system spec (local-first architecture) |
| `main.py` | Entry point with full startup sequence |
| `core/config.py` | Env-driven config (SQLITE_PATH, WEB_HOST, safe mode flags) |
| `core/types.py` | All frozen dataclasses and enums |
| `core/errors.py` | Typed error hierarchy (SafeModeBlockedError, exchange errors) |
| `exchange/transport.py` | HMAC-SHA512 signing, HTTP transport with retry |
| `exchange/parsers.py` | Kraken JSON → domain types + mutation response parsers |
| `exchange/executor.py` | Full Kraken executor (read + write: orders, cancels, ws_token) |
| `exchange/order_gate.py` | cl_ord_id generation, circuit breaker |
| `exchange/websocket.py` | Kraken WS v2 (connect, ticker, executions, FallbackPoller) |
| `exchange/ws_parsers.py` | Pure WS message parsing (PriceTick, FillConfirmed) |
| `exchange/models.py` | KrakenOrder, KrakenTrade, KrakenState |
| `persistence/sqlite.py` | SQLite adapter (WAL, SqliteReader + SqliteWriter, ledger) |
| `trading/reconciler.py` | Kraken ↔ recorded state reconciliation + DB seeding |
| `trading/risk_rules.py` | Position + portfolio risk checks |
| `guardian.py` | Stop/target monitoring, risk enforcement |
| `scheduler.py` | Main loop orchestration |
| `web/app.py` | FastAPI + SSE local dashboard |
| `healing/watchdog.py` | Advisory watchdog with optional Ollama |

## Environment

- **Platform**: Windows 11, Git Bash
- **Python**: 3.12 at C:/Users/ColsonR/AppData/Local/Programs/Python/Python312
- **Deployment**: Spare laptop (bot + SQLite + dashboard), Tailscale for remote
- **Subscriptions**: Claude Max + Codex Max (flat-rate, $0 marginal LLM cost)
- **Cross-agent runner**: C:/Users/ColsonR/Agent/runner/cross-agent.py
- **Auto-research strategy**: C:/Users/ColsonR/auto-researchtrading
- **Repo**: git@github.com:robjohncolson/kraken-bot-v4.git
