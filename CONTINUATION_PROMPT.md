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

## What to do NOW: Feature engineering and better baselines

Phases 0-5a are complete. The LLM path is proven infrastructure but does not beat logistic regression. Next research effort should focus on feature engineering, calibration, and stronger baselines — not LLM tuning.

### TUI Operator Cockpit (v1, completed 2026-03-26)

**Launch**: `python -m tui` (or `TUI_BASE_URL=http://host:port python -m tui`)

**Package**: `tui/` — fully isolated from `web/` and runtime, consumes existing dashboard API + SSE.

Screens (keyboard-navigable):
- `1` Overview (health, portfolio, positions, orders, beliefs, reconciliation, event log)
- `2` Positions (full table with pair/side/qty/entry/stop/target/price/P&L/grid)
- `3` Beliefs (matrix by pair and source with direction/confidence/regime)
- `4` Orders (open + pending orders)
- `5` Reconciliation (discrepancy/ghost/foreign/untracked/fee drift)
- `6` Event Log (recent bot events, ring buffer)
- `?` Help (key bindings + color legend)
- `r` manual refresh, `p` pause/resume, `[`/`]` pair navigation, `q` quit

Data flow: initial snapshot from `/api/*` endpoints, live updates from `/sse/updates` with exponential backoff reconnect. SSE disconnect shows degraded banner, never crashes.

No backend changes required — the TUI consumes the existing read model. If future TUI features need new fields (pending orders detail, cooldowns, heartbeat summary), extend the shared dashboard read model first.

Tests: 54 new tests (state parsers, SSE parser, theme helpers, Textual app navigation). 447 total passing.

### Completed phases

- **Phase 0**: renamed autoresearch → technical_ensemble
- **Phase 1**: research dataset export in kraken-bot-v4 (`research/` module)
- **Phase 2**: walk-forward evaluation harness in autoresearch (`trading_eval/` package)
- **Phase 3**: baselines established (TA ensemble, logistic regression, GBT)
- **Phase 4**: artifact contract defined, synced to kraken-bot-v4
- **Phase 5a**: LLM candidate evaluated — logistic regression wins (see below)

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

### Phase 5a result (evaluated 2026-03-26)

**Model**: qwen3:8b (Q4_K_M, 5.2GB) via IPEX-Ollama 0.9.3 with Intel Arc GPU (SYCL/oneAPI)
**Runtime**: ~21s per inference call on Intel Arc iGPU, GPU-accelerated via IPEX-LLM
**Contract**: direction/confidence/prob_up/horizon_hours=6, Ollama JSON mode
**Decision cadence**: 6h (one prediction per horizon period, no overcounting)

Walk-forward results (5-fold, 10d train, 1d val, 5d step, DOGE/USD):

| Candidate | Accuracy | Net P&L (bps) | Sharpe | Hit Rate | Trades |
|-----------|----------|---------------|--------|----------|--------|
| Logistic regression | 67.6% | +2,838 | 29.6 | 63.2% | 68 |
| LLM (Qwen3 8B) | 62.5% | -167 | -9.3 | 50.0% | 8 |
| GBT | 44.3% | -2,732 | -19.5 | 43.2% | 88 |
| TA ensemble | 0% | 0 | 0 | 0% | 0 |

**Verdict**: Prompted LLM does not beat logistic regression. Infrastructure is proven (structured output works, GPU path viable, contract enforced), but there is no signal advantage. TA ensemble produces 0 trades on this window size (needs 40 bars history, validation window too short).

**Decision**:
- Logistic regression is the incumbent research winner
- LLM candidate remains as proven infrastructure for future use
- Phase 5b (fine-tuning) is paused — not justified by current results
- Next research effort: feature engineering, calibration, stronger baselines

### Recommended next research directions

1. **Feature engineering**: add more engineered features to the sklearn baselines (momentum indicators, volatility regimes, volume profiles, cross-timeframe features)
2. **Calibration**: apply Platt scaling or isotonic regression to baseline probability outputs
3. **Longer evaluation windows**: run with more data (90d train, broader date range)
4. **Fix TA ensemble**: pass training tail to predict() so it has enough history for signals
5. **Only revisit LLM if**: feature-enriched prompt or news/sentiment data gives a fundamentally different input than raw OHLCV

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
- **TUI Cockpit** ✅ Read-only operator cockpit (Textual + Rich, 7 screens, SSE live, 54 tests)
- **Phase 0** ✅ Renamed autoresearch → technical_ensemble
- **Research specs** ✅ Codex-authored integration spec + implementation checklist
- **Phase 1** ✅ Research dataset export (OHLCV history, DB reader, labels, builder, CLI, 34 tests)

## Session Commits (2026-03-26, Phase 5a evaluation)

### kraken-bot-v4
```
a584999 fix: deduplicate OHLCV timestamps across pagination boundaries
```

### autoresearch
```
4dbb288 build(phase-5a): complete LLM evaluation — logistic regression wins
2697633 build(phase-5a): upgrade default LLM to qwen3.5:9b
b732d54 build(phase-5a): add LLM candidate via Ollama with 6h decision cadence
```

### Previous sessions
```
# autoresearch (Phase 2-4, 2026-03-25)
6398c0e build(phase-4): add artifact schema, promotion workflow, and CLI commands
6aa2636..f9ce514 build(phase-2/3): walk-forward harness, baselines, storage, CLI

# kraken-bot-v4 (Phase 2-4 + TUI, 2026-03-25/26)
94a10ce docs: update continuation prompt after Phases 2-4 completion
3682eb0 docs(phase-4): add artifact contract v1 for research model integration
```

## Current State

- **kraken-bot-v4 branch**: master, at `a584999`
- **autoresearch branch**: master, at `4dbb288`
- **kraken-bot-v4 tests**: 447 passed (393 existing + 54 TUI), ruff clean
- **autoresearch tests**: 119 passed, 11 skipped (parity tests, Python 3.10 vs 3.11)
- **Trading bot**: unchanged, still live-capable with TA ensemble beliefs
- **Evaluation harness**: fully operational with 4 candidates (TA, LogReg, GBT, LLM)
- **Incumbent winner**: logistic regression (+2,838 bps, 67.6% accuracy, Sharpe 29.6)
- **LLM path**: proven infrastructure, does not beat LogReg, Phase 5b paused
- **Artifact contract**: defined and synced to kraken-bot-v4
- **IPEX-Ollama**: working at `C:\Users\rober\ipex-ollama\` with Intel Arc GPU acceleration

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
| `tui/app.py` | TUI operator cockpit (Textual App, key bindings, screen switching) |
| `tui/client.py` | Dashboard HTTP client (snapshot fetch) |
| `tui/events.py` | SSE stream reader (async generator) |
| `tui/state.py` | Presentation state + JSON parsers |
| `tui/screens/` | 7 screens: overview, positions, beliefs, orders, reconciliation, logs, help |
| `tui/widgets/` | Reusable widgets: health, portfolio, positions, beliefs, orders, reconciliation, event_log, status_bar |
| `docs/specs/tui-operator-cockpit-spec.md` | TUI v1 spec |

## Environment

- **Platform**: Windows 11, Git Bash
- **Python**: 3.13
- **Deployment**: Spare laptop (bot + SQLite + dashboard), Tailscale for remote
- **Subscriptions**: Claude Max + Codex Max ($0 marginal LLM cost)
- **Repo**: git@github.com:robjohncolson/kraken-bot-v4.git
- **Known issue**: School network may filter Kraken API — use hotspot for live tests
