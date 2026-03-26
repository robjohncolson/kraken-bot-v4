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

## What to do NOW: Phase 5 — Qwen Research Path

Phases 0-4 are complete. The next milestone is Phase 5: adding a Qwen-class model as a structured forecast candidate in the evaluation harness.

### Completed phases

- **Phase 0**: renamed autoresearch → technical_ensemble
- **Phase 1**: research dataset export in kraken-bot-v4 (`research/` module)
- **Phase 2**: walk-forward evaluation harness in autoresearch (`trading_eval/` package)
- **Phase 3**: baselines established (TA ensemble, logistic regression, GBT)
- **Phase 4**: artifact contract defined, synced to kraken-bot-v4

### Phase 2 deliverables (in autoresearch)

- `trading_eval/config.py` — EvalConfig dataclass
- `trading_eval/data.py` — manifest-validated Parquet loader
- `trading_eval/splitter.py` — walk-forward time-series splitter
- `trading_eval/candidate.py` — Candidate ABC with timestamp-keyed predictions
- `trading_eval/backtest.py` — backtest engine with fees, slippage, abstain
- `trading_eval/metrics.py` — direction accuracy, Brier, P&L, Sharpe, drawdown
- `trading_eval/runner.py` — experiment orchestration
- `trading_eval/storage.py` — structured experiment records with reproducibility metadata
- `trading_eval/artifact.py` — artifact schema and promotion workflow
- `trading_eval/cli.py` — CLI: run, list, compare, promote, artifacts
- `trading_eval/baselines/ta_ensemble.py` — standalone 6-signal TA port
- `trading_eval/baselines/sklearn_baseline.py` — LogReg + GBT baselines
- `trading_eval/baselines/run_baselines.py` — baseline runner + comparison
- 102 tests passing, 11 parity tests (skip on Python 3.10)

### Phase 4 deliverables

- `trading_eval/artifact.py` — ArtifactManifest + promote_candidate()
- `kraken-bot-v4/docs/specs/artifact-contract-v1.md` — consumer interface spec

### Phase 5 scope

Add Qwen structured forecast candidate in autoresearch:
- Structured inference format (direction, confidence, regime, horizon_hours)
- Constrained JSON output, no free-form text
- Evaluate prompt-only and fine-tuned variants separately
- Calibrate confidence after raw model scoring
- Only promote if it beats simpler baselines out of sample

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
- **Phase 1** ✅ Research dataset export (OHLCV history, DB reader, labels, builder, CLI, 34 tests)

## Session Commits (2026-03-25, Phase 2-4 build)

### kraken-bot-v4
```
3682eb0 docs(phase-4): add artifact contract v1 for research model integration
```

### autoresearch
```
6398c0e build(phase-4): add artifact schema, promotion workflow, and CLI commands
6aa2636 build(phase-3): add baseline runner script with comparison table
f7f36f0 build(phase-3): add logistic regression and GBT baselines
1fa40ab build(phase-3): add standalone TA ensemble baseline with parity test
29f82c4 build(phase-2): add CLI with run/list/compare subcommands
f6b7925 build(phase-2): add experiment storage with reproducibility metadata
ac5badd build(phase-2): add experiment runner with walk-forward orchestration
3869f5c build(phase-2): add metrics computation (accuracy, Brier, P&L, Sharpe, drawdown)
5948bc3 build(phase-2): add backtest engine with fees, slippage, and abstain support
4aca055 build(phase-2): add candidate protocol with timestamp-keyed predictions
001ec27 build(phase-2): add walk-forward time-series splitter
f9ce514 build(phase-2): add trading_eval package skeleton with config and data loader
```

### Previous sessions (kraken-bot-v4)
```
0fd6afc build(phase-10): implement research dataset export module
eb0cd77 build: add phase-10 manifest for research dataset export
dcf75da fix: make build harness runner path configurable via CROSS_AGENT_RUNNER env
689c3c1 build: serve dashboard HTML + add autoresearch integration specs
eca8508 refactor: rename autoresearch to technical_ensemble (Phase 0)
```

## Current State

- **kraken-bot-v4 branch**: master, at `3682eb0`
- **autoresearch branch**: master, at `6398c0e`
- **kraken-bot-v4 tests**: 393 passed, ruff clean
- **autoresearch tests**: 102 passed, 11 skipped (parity tests, Python 3.10 vs 3.11)
- **Trading bot**: unchanged, still live-capable with TA ensemble beliefs
- **Evaluation harness**: fully operational in autoresearch
- **Baselines**: TA ensemble, logistic regression, GBT — all runnable
- **Artifact contract**: defined and synced to kraken-bot-v4

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
| `research/ohlcv_history.py` | Paginated Kraken OHLCV fetch with timestamps |
| `research/db_reader.py` | SQLite reader for fills/orders/closed trades (research) |
| `research/labels.py` | Forward-looking labels (return_sign/bps, regime) |
| `research/dataset_builder.py` | Orchestrates Parquet + manifest export |
| `research/cli.py` | CLI: python -m research.cli |
| `build/manifests/phase-10.research-dataset.json` | Phase 10 task manifest |
| `web/app.py` | FastAPI + SSE + static file serving |

## Environment

- **Platform**: Windows 11, Git Bash
- **Python**: 3.13
- **Deployment**: Spare laptop (bot + SQLite + dashboard), Tailscale for remote
- **Subscriptions**: Claude Max + Codex Max ($0 marginal LLM cost)
- **Repo**: git@github.com:robjohncolson/kraken-bot-v4.git
- **Known issue**: School network may filter Kraken API — use hotspot for live tests
