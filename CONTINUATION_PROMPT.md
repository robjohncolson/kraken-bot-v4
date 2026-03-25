# Continuation Prompt — kraken-bot-v4

## Architecture (local-first, decided 2024-03-24)

- **Spare laptop at home**: single always-on runtime host
- **Kraken**: truth for live balances/orders/fills
- **SQLite** (`./data/bot.db`): durable coordination store (WAL mode)
- **Local JSONL**: audit/recovery trail (ledger, snapshots, offline queue)
- **FastAPI dashboard**: local on bot host (`localhost:8080`)
- **Tailscale**: remote access from school
- **No Supabase** in runtime path (legacy code retained, not used)
- **No Railway** (dashboard is local)

## What to do NOW: First controlled run

Tasks 1, 2A, and 3 are done. The bot can start up, fetch live Kraken state, read SQLite recorded state, and run real reconciliation.

### Completed tasks:

- **Task 1** ✅ `.env.example`, `main.py` entry point, safe mode flags (READ_ONLY_EXCHANGE, DISABLE_ORDER_MUTATIONS, STARTUP_RECONCILE_ONLY — all default true)
- **Task 2A** ✅ Authenticated read-only Kraken REST (transport with HMAC-SHA512 signing, parsers with `.F`/`.S` suffix stripping, executor, strictly-increasing nonce)
- **Task 3** ✅ Local-first migration (Supabase types renamed to RecordedPosition/Order/State, SQLite adapter, config flipped, SPEC.md updated)

### Priority tasks remaining (in order):

**1. First smoke test with real `.env`**
- Copy `.env.example` to `.env`, fill in `KRAKEN_API_KEY` + `KRAKEN_API_SECRET`
- Run `python main.py` with `STARTUP_RECONCILE_ONLY=true`
- Should: fetch Kraken balances/orders/trades, read empty SQLite, reconcile (all Kraken assets flagged as "untracked"), exit cleanly
- Fix any issues found

**2. Task 2B: Kraken mutation execution**
- `execute_order()` / `execute_cancel()` on executor
- `cl_ord_id`-aware retry (no blind retry on mutations)
- Safe mode enforcement (`SafeModeBlockedError`)
- Mutation parsers (`parse_add_order`, `parse_cancel_order`)

**3. Task 3B: SQLite write support**
- Upsert positions, orders from reconciliation results
- Ledger writes
- So startup reconciliation can seed the DB on first run

**4. Wire `exchange/websocket.py` to Kraken WebSocket v2**
- Price feeds (ticker, ohlc) and private feeds (executions, openOrders)
- REST polling fallback when WebSocket drops

**5. First controlled run**
- Minimum position sizes, DOGE/USD as first pair
- Verify: startup reconciliation, heartbeat writing, guardian loop, grid activation
- Dashboard on localhost via Tailscale

### Key architectural decisions:

- Spare laptop = single runtime authority (no split state)
- Kraken native stop-loss = only market order exception
- All other orders = limit, +/- 0.4% for maker fees
- Beliefs: Claude Code CLI + Codex CLI + auto-research 6-signal ensemble, 2/3 agreement
- Grid: V2 S0/S1a/S1b/S2 states, no per-slot identity, profit redistribution
- Stats: AP Stats parametric first, normality gate, n>=30, fail closed
- Safe mode defaults to all-on (must opt in to live trading)

## Session Commits (2024-03-24)

```
4cfe5b4 docs(task-3): complete SPEC.md local-first migration
36245d1 docs(task-3): update SPEC.md and dashboard to local-first architecture
8ac73c6 build(task-3): flip config and startup to local-first SQLite
143ec50 build(task-3): add SQLite persistence adapter
5cb2fc4 refactor(task-3): rename Supabase runtime types to storage-agnostic names
ad7cbab docs: add session continuation prompt from initial build session
11ba30c build(task-2a): authenticated read-only Kraken REST client
6e1e8e2 fix(task-1): address review findings
e58c239 build(task-1): add .env.example, main.py entry point, safe mode flags
```

## Previous Session Commits (initial scaffold build)

```
aa6f466 fix(phase-6): remove unused WatchdogAnalyzer import
c073986 build(phase-6): complete self-healing skeleton with optional Ollama watchdog
2f7c599 build(phase-5): complete observability phase
c2af597 build(phase-4): complete trading phase
1938d91 build(phase-3): complete belief formation phase
bc5e030 build(scaffold): add spec and local build orchestration
7cbc97a build(auto-review): eliminate manual CC↔Codex relay
```

## Current State

- **Branch**: master, pushed to origin at 4cfe5b4
- **Tests**: 268 passed, ruff clean
- **Architecture**: local-first (SQLite + local dashboard + Tailscale)
- **Startup path**: config → SQLite → Kraken health → Kraken state → recorded state → reconcile → exit or loop
- **Safe mode**: all flags default to true
- **Untracked**: chatGPT_analysis.md, claude_analysis.md, grok_analysis.md, doge-bot-env.txt, grid-bot-v2-envs.txt

## Key Paths

| File | Purpose |
|------|---------|
| `SPEC.md` | Full system spec (local-first architecture) |
| `main.py` | Entry point with full startup sequence |
| `core/config.py` | Env-driven config (SQLITE_PATH, WEB_HOST, safe mode flags) |
| `core/types.py` | All frozen dataclasses and enums |
| `exchange/transport.py` | HMAC-SHA512 signing, HTTP transport with retry |
| `exchange/parsers.py` | Kraken JSON → domain types (Balance, KrakenOrder, KrakenTrade) |
| `exchange/executor.py` | Read-only Kraken executor (fetch_kraken_state) |
| `exchange/models.py` | KrakenOrder, KrakenTrade, KrakenState |
| `persistence/sqlite.py` | SQLite adapter (WAL, schema, SqliteReader) |
| `persistence/supabase.py` | Legacy (unused, retained for reference) |
| `trading/reconciler.py` | Kraken ↔ recorded state reconciliation |
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
