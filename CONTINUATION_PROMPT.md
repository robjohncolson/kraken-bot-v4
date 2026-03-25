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

## What to do NOW: First controlled writable run on laptop

The school network intermittently filters Kraken API traffic. All remaining live tests must run on the spare laptop (hotspot).

### Setup on laptop

1. `git pull` to get commit `d6ee7da`
2. Create `.env` with Kraken keys (see `kraken-bot-v4-env.txt` on school machine, NOT committed):
   ```
   KRAKEN_API_KEY=FPJlgctDrBoz6PkKuDT11/S/nH3mGs9SbL4McRtQEdPdOab6iuUGbnjO
   KRAKEN_API_SECRET=3rUOnXkAgtjXtYs+rLxcCJDLPqxJ3jr7WB09sGk7H6VKc1blGmq6yiEqXFTyPJQcD99H8kKEZgMT+f+7K2yumw==
   ```
   Plus all other defaults from `.env.example`. Safe mode flags stay ON in `.env`.
3. `pip install python-dotenv websockets httpx` (if not already installed)
4. Run smoke test first: `python main.py` (reconcile-only, should exit cleanly)

### Writable run (shell overrides, don't edit .env)

```powershell
$env:STARTUP_RECONCILE_ONLY='false'
$env:READ_ONLY_EXCHANGE='false'
$env:DISABLE_ORDER_MUTATIONS='false'
$env:ALLOWED_PAIRS='DOGE/USD'
$env:MAX_POSITION_USD='10'
$env:MIN_POSITION_USD='10'
$env:WEB_PORT='58392'
python main.py
```

### What to expect

The state machine reducer (`core/state_machine.py:33`) is a **complete no-op** — returns NO_ACTIONS for every event. So even with mutations enabled, the bot will NOT place orders. The writable run validates:
- Kraken connectivity (REST + WebSocket) without school network filtering
- Authenticated session stays healthy
- Dashboard/heartbeat/guardian run clean for 2+ cycles
- Pair whitelist logs `DOGE/USD` in startup banner
- No `SafeModeBlockedError` (proves wiring fix works)

### Success criteria

1. Startup banner: no safe mode flags + `Pair whitelist: DOGE/USD`
2. Reconciliation completes (untracked assets warning is fine)
3. 2+ scheduler cycles with no errors
4. Heartbeat: `bot_status: healthy`, `websocket_connected: true`
5. Dashboard: `/api/health` and `/api/reconciliation` respond
6. Zero AddOrder/CancelOrder in logs

### Abort conditions

- `SafeModeBlockedError` (wiring bug)
- `PairNotAllowedError` for DOGE/USD (normalization bug)
- Any `AddOrder`/`CancelOrder` in logs
- `DEGRADED` heartbeat for >2 cycles
- Repeated `ExchangeError`

### After writable run passes

The next real milestone is **implementing the reducer** — making the state machine produce actual PlaceOrder/CancelOrder actions based on belief signals and grid state. The no-op reducer is the only thing standing between "runtime works" and "bot trades."

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
- **Smoke test** ✅ Reconcile-only against real Kraken (school machine)
- **Read-only runtime** ✅ Dashboard, WebSocket, heartbeat verified (school machine)
- **Writable run** ⏳ Inconclusive on school network — needs laptop retest

## Session Commits (2026-03-25)

```
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

- **Branch**: master, pushed at `d6ee7da`
- **Tests**: 330 passed, ruff clean
- **Kraken account**: ~5,522 DOGE, $0.009 USD (dust), 0 open orders
- **Reducer**: no-op (core/state_machine.py:33) — no actions produced
- **Untracked**: chatGPT_analysis.md, claude_analysis.md, grok_analysis.md

## Key Paths

| File | Purpose |
|------|---------|
| `SPEC.md` | Full system spec (local-first architecture) |
| `main.py` | Entry point — wires settings into executor + runtime |
| `runtime_loop.py` | WebSocket, dashboard, heartbeat, scheduler integration |
| `core/config.py` | Env-driven config (includes ALLOWED_PAIRS) |
| `core/state_machine.py` | **No-op reducer** — next major implementation target |
| `exchange/order_gate.py` | Single mutation gate with pair whitelist |
| `exchange/executor.py` | Kraken executor (read + write, safe mode enforcement) |
| `exchange/websocket.py` | Kraken WS v2 (ticker, executions, fallback) |
| `persistence/sqlite.py` | SQLite adapter (WAL, reader + writer) |
| `trading/reconciler.py` | Kraken vs recorded state reconciliation |
| `web/app.py` | FastAPI + SSE local dashboard |

## Environment

- **Platform**: Windows 11, Git Bash
- **Python**: 3.12
- **Deployment**: Spare laptop (bot + SQLite + dashboard), Tailscale for remote
- **Subscriptions**: Claude Max + Codex Max ($0 marginal LLM cost)
- **Repo**: git@github.com:robjohncolson/kraken-bot-v4.git
- **Known issue**: School network may filter Kraken API — use hotspot for live tests
