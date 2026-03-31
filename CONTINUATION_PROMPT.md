# Continuation Prompt — kraken-bot-v4

## Architecture

- **Host**: spare laptop at home, always-on
- **Exchange**: Kraken (Starter tier) — source of truth for balances/orders/fills
- **Persistence**: SQLite (`./data/bot.db`, WAL mode) — positions, orders, ledger, cooldowns, rotation tree
- **Dashboard**: FastAPI + D3.js at `http://0.0.0.0:58392` (LAN-accessible)
- **TUI**: `python -m tui` — Textual/Rich operator cockpit, 7 screens
- **Beliefs**: 3 available models (select via `BELIEF_MODEL` env var)
- **Platform**: Windows 11, Python 3.13, WSL for runtime
- **Repo**: `git@github.com:robjohncolson/kraken-bot-v4.git`, branch `master`

## Current live state (as of 2026-03-31)

Bot running on WSL Athena pane: `/mnt/c/Python313/python.exe main.py`

| Field | Value |
|-------|-------|
| Belief model | `llm_council` (CC+Codex via tmux-bridge) |
| Poll interval | 1 hour (`BELIEF_STALE_HOURS=2`) |
| Portfolio | ~$500 (4,651 DOGE + $80 USD) |
| Open positions | 0 |
| Dashboard | `http://10.0.0.24:58392` |
| Tests | 500+ passing |

## Belief models

| Model | Env value | How it works |
|-------|-----------|-------------|
| `technical_ensemble` | Default | 6-signal TA (EMA, RSI, MACD, Bollinger, momentum) |
| `research_model` | Requires `ACTIVE_ARTIFACT_ID` | V1 LogReg (+5,531 bps on 180d backtest, all rollout gates pass) |
| `llm_council` | Requires broker sidecar | CC+Codex analyze structured market context via file-based messaging |

### LLM Council

- Handler: `beliefs/llm_council_handler.py` — builds market context, writes request files
- Broker: `python scripts/llm_council_broker.py` — dispatches to CC+Codex panes, collects votes
- Protocol: `state/llm-council/{requests,responses,consensus}/*.json`
- Consensus: 2/2 agree = that direction, split = neutral

### Research model artifact

`artifacts/logistic_regression_20260329_3f73bb8a/` — 7-feature V1 LogReg, threshold 0.55, CryptoCompare-backed 180d dataset.

Backfill validation: +4,862 bps, 55.1% accuracy, 100% coverage, all rollout gates pass.

## Recursive rotation tree

**Spec**: `docs/specs/recursive-rotation-tree-spec.md`

**Vision**: Denomination-agnostic recursive trading. Portfolio holdings become root nodes. Each asset scans all Kraken pairs for bear exits / bull entries. Rotations create timed child nodes. Children recurse within parent windows. Confidence-weighted sizing.

**Foundation complete (R1-R4)**:
- `core/types.py`: RotationNode, RotationCandidate, RotationTreeState
- `trading/rotation_tree.py`: Pure helpers (build_root_nodes, compute_child_allocations, cascade_close, etc.)
- `trading/rotation_planner.py`: RotationTreePlanner (initialize_roots, plan_cycle)
- `trading/pair_scanner.py`: Generalized — discover_asset_pairs(source_asset), scan_rotation_candidates()
- `persistence/sqlite.py`: rotation_nodes table, save/fetch
- `runtime_loop.py`: Planner wired into run_once(), auto-persists

**NOT live yet** — execution layer gaps (Codex review):
1. PLANNED nodes → PlaceOrder reducer bridge (don't treat PLANNED as holdings)
2. Fill settlement: bind fills to nodes, convert qty to destination denomination
3. Expiry → real ClosePosition effects (currently in-memory cascade only)
4. Root init needs REST price fetch for non-USD assets
5. Generalize portfolio accounting beyond cash_usd/cash_doge

Activate: `ENABLE_ROTATION_TREE=true` once execution wiring is complete.

## Key infrastructure

| Feature | Status |
|---------|--------|
| Position persistence | Done — survives restart via SQLite |
| Exit price | Uses trigger/reference + 0.1% marketable offset |
| WebSocket | Re-enabled for home laptop |
| Reconciliation | Uses exchange open orders (not bot_state) |
| D3 dashboard | Wired — portfolio cards, positions table, belief heatmap, reconciliation |
| TUI | 7 screens, live SSE, keyboard navigation |
| Conditional tree (v1) | Built, disabled by default (`ENABLE_CONDITIONAL_TREE`) |
| Backfill shadow eval | `python -m research.backfill_shadow` |

## Running the bot

```bash
# Launch bot (from WSL)
cd /mnt/c/Users/rober/Downloads/Projects/kraken-bot-v4
/mnt/c/Python313/python.exe main.py

# Launch TUI (separate terminal)
/mnt/c/Python313/python.exe -m tui

# Launch LLM Council broker (if using llm_council belief model)
/mnt/c/Python313/python.exe scripts/llm_council_broker.py
```

### Key env vars (.env)

```
KRAKEN_API_KEY=...
KRAKEN_API_SECRET=...
BELIEF_MODEL=llm_council          # or technical_ensemble, research_model
BELIEF_STALE_HOURS=2              # poll every 1 hour
ALLOWED_PAIRS=DOGE/USD
WEB_HOST=0.0.0.0
WEB_PORT=58392
READ_ONLY_EXCHANGE=false
DISABLE_ORDER_MUTATIONS=false
STARTUP_RECONCILE_ONLY=false
MIN_POSITION_USD=10
MAX_POSITION_USD=10
EXIT_LIMIT_OFFSET_PCT=0.1
```

## Research results summary

| Model | P&L (bps) | Sharpe | Accuracy | Trades |
|-------|-----------|--------|----------|--------|
| V1 LogReg (180d) | **+5,531** | **11.3** | 47.2% | 214 |
| TA Ensemble (180d) | +257 | 0.27 | 48.9% | 380 |
| Backfill shadow | +4,862 | — | 55.1% | 1,548 |
| LLM Qwen3 (30d) | -167 | -9.3 | 62.5% | 8 |

## Goal for next session

Wire rotation tree execution layer:
1. Reducer bridge: PLANNED nodes → PlaceOrder effects
2. Fill settlement with denomination conversion
3. Expiry → real ClosePosition effects
4. REST price fetch for root init
5. Portfolio generalization beyond USD/DOGE

## Validation

```bash
python -m pytest                    # 500+ tests
python -m ruff check .              # clean
curl http://127.0.0.1:58392/api/health   # dashboard up
```
