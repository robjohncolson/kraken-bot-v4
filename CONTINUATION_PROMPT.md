# Continuation Prompt — kraken-bot-v4

## What to do NOW: Wire up for first controlled run

All 6 SPEC.md phases are scaffolded (36 tasks, 217 tests, all passing, ruff clean). The next step is connecting the scaffolding to real infrastructure for a first controlled run with minimum position sizes on live Kraken.

### Priority tasks (in order):

**1. Create `.env` template and `main.py` entry point**
- `.env.example` with all required keys (KRAKEN_API_KEY, KRAKEN_API_SECRET, SUPABASE_URL, SUPABASE_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- `main.py` wiring: load config → connect Supabase → connect Kraken → reconcile → start scheduler loop
- The startup sequence is defined in SPEC.md under "Persistence" section 8

**2. Wire `exchange/client.py` to real Kraken REST API**
- Current state: rate limiter skeleton only, no actual HTTP calls
- Needs: authenticated REST calls (AddOrder, CancelOrder, OpenOrders, QueryOrders, Balance, TradesHistory)
- Kraken Starter tier: 15 bucket max, -0.33/s decay, +8 cancel penalty if order < 5s old
- Symbol normalization already exists in `exchange/symbols.py`

**3. Wire `exchange/websocket.py` to real Kraken WebSocket v2**
- Price feeds (ticker, ohlc) and private feeds (executions, openOrders)
- REST polling fallback when WebSocket drops

**4. Wire `persistence/supabase.py` to real Supabase PostgreSQL**
- Current state: offline queue skeleton only
- Needs: real client using `supabase-py` or direct `psycopg2` (SPEC.md recommends direct Postgres connection via Supavisor port 5432 for low latency)
- Tables: positions, orders, beliefs, grid_cycles, config, ledger

**5. First controlled run**
- Minimum position sizes, DOGE/USD as first pair
- Verify: startup reconciliation, heartbeat writing, guardian loop, grid activation
- Dashboard on Railway reading from Supabase

### Key architectural decisions already made:
- Laptop runs trading + beliefs + Claude Code/Codex (not Railway)
- Railway = read-only D3 dashboard only
- Supabase = source of truth
- Kraken native stop-loss = only market order exception
- All other orders = limit, +/- 0.4% for maker fees
- Beliefs: Claude Code CLI + Codex CLI + auto-research 6-signal ensemble, 2/3 agreement
- Grid: V2 S0/S1a/S1b/S2 states, no per-slot identity, profit redistribution
- Stats: AP Stats parametric first, normality gate, n>=30, fail closed

## Session Commits (this session)

```
aa6f466 fix(phase-6): remove unused WatchdogAnalyzer import
c073986 build(phase-6): complete self-healing skeleton with optional Ollama watchdog
[6.1-6.5] Heartbeat, incidents, analyzer, Ollama, watchdog
2f7c599 build(phase-5): complete observability phase
[5.1-5.6] FastAPI, routes, static frontend, D3 grid/beliefs, Telegram
c2af597 build(phase-4): complete trading phase
[4.1-4.7] Position, sizing, portfolio, risk rules, reconciler, guardian, scheduler
1938d91 build(phase-3): complete belief formation phase
[3.1-3.6] Prompts, autoresearch, Claude/Codex sources, consensus, orchestrator
bc5e030 build(scaffold): add spec and local build orchestration
[1.1-1.7] Config, types, state machine, normality, symbols, client, order gate, supabase
[2.1-2.5] Grid states, transitions, sizing, accounting, engine
7cbc97a build(auto-review): eliminate manual CC↔Codex relay
```

## Current State

- **Branch**: master, pushed to origin at aa6f466
- **Tests**: 217 passed, ruff clean
- **Build system**: `build/run_phase.py` with auto-review (CC→Codex review via cross-agent.py)
- **All phases**: done (state files in `build/state/`)
- **Untracked**: chatGPT_analysis.md, claude_analysis.md, grok_analysis.md (intentional research files)

## Key Paths

| File | Purpose |
|------|---------|
| `SPEC.md` | Full system spec (644 lines) |
| `build/BUILD_SYSTEM_DESIGN.md` | Build loop design doc |
| `build/run_phase.py` + `build/common.py` | Build orchestration with auto-review |
| `core/types.py` | All frozen dataclasses and enums (367 lines) |
| `core/state_machine.py` | Pure reducer skeleton |
| `grid/states.py` | Grid S0/S1a/S1b/S2 lifecycle |
| `grid/engine.py` | Grid activation, deactivation, redistribution |
| `beliefs/orchestrator.py` | Belief source coordination + consensus |
| `trading/reconciler.py` | Kraken ↔ Supabase reconciliation |
| `trading/risk_rules.py` | Position + portfolio risk checks |
| `guardian.py` | Stop/target monitoring, risk enforcement |
| `scheduler.py` | Main loop orchestration |
| `web/app.py` | FastAPI + SSE dashboard shell |
| `healing/watchdog.py` | Advisory watchdog with optional Ollama |

## Environment

- **Platform**: Windows 11, Git Bash
- **Python**: 3.12 at C:/Users/ColsonR/AppData/Local/Programs/Python/Python312
- **Deployment**: Laptop (trading) + Railway (dashboard) + Supabase (source of truth)
- **Remote access**: Tailscale from school
- **Subscriptions**: Claude Max + Codex Max (flat-rate, $0 marginal LLM cost)
- **Cross-agent runner**: C:/Users/ColsonR/Agent/runner/cross-agent.py
- **Auto-research strategy**: C:/Users/ColsonR/auto-researchtrading
- **Repo**: git@github.com:robjohncolson/kraken-bot-v4.git
