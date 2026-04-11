# Continuation Prompt — kraken-bot-v4

## Architecture

```
CC Brain (scripts/cc_brain.py) — runs every 2h or on-demand
  ├── Reads temporal memory (what happened last time)
  ├── Observes portfolio (balances, positions)
  ├── Analyzes pairs:
  │     ├── HMM regime detection (trending/ranging/volatile)
  │     ├── RSI(14) + EMA(7/26) on 1H and 4H bars
  │     └── Kronos (24h full-OHLCV candle prediction, Intel Arc GPU)
  ├── Scores entries (gates: regime > 0.40, 4H up, RSI oversold)
  ├── Places orders or sits out
  ├── Writes memories (decisions, regime, snapshots)
  └── Generates review report

Bot (always-on, deterministic body)
  ├── WebSocket price streaming
  ├── TP/SL/trailing stop monitoring
  ├── Fill settlement + reconciliation
  └── REST API for CC to read/write
```

- **Control split**: Bot = dumb body. CC = brain. `CC_BRAIN_MODE=true` disables bot's autonomous planner.
- **Host**: spare laptop at home, always-on
- **Exchange**: Kraken (Starter tier) — source of truth for balances/orders/fills
- **Persistence**: SQLite (`./data/bot.db`, WAL mode) — positions, orders, ledger, cooldowns, rotation tree, pair_metadata, cc_memory
- **Dashboard**: FastAPI + D3.js at `http://0.0.0.0:58392` (LAN-accessible)
- **TUI**: `python -m tui` — Textual/Rich operator cockpit, 8 screens (key 7 = Rotation Tree)
- **Platform**: Windows 11, Python 3.13, Intel Arc GPU (torch 2.8.0+xpu)
- **Repo**: `git@github.com:robjohncolson/kraken-bot-v4.git`, branch `master`

## Current state (as of 2026-04-11)

**CC IS THE BRAIN** — Bot runs as deterministic body, CC makes all trading decisions.

| Field | Value |
|-------|-------|
| CC Brain Mode | `CC_BRAIN_MODE=true` — bot's planner + root evaluator disabled |
| Belief model | `timesfm` (still wired but CC uses its own signals) |
| CC Signals | RSI(14) + EMA(7/26) + Kronos + HMM regime |
| Portfolio | ~$152 cash, 6 open roots (ASTER/AZTEC/BANANAS31 = dust, CRV/GBP/USD = real) |
| First CC trade | AVAX/USD limit buy @ $9.22 (txid OHJ2OJ-4LIFS-UBFFUD) |
| CC Memory | 17+ events (decisions, observations, regimes, post-mortems, param changes) |
| Tests | **676+ passing** |
| MTF gates | `MTF_4H_GATE_ENABLED=true`, `MTF_15M_CONFIRM_ENABLED=true` |
| HMM regime | 3-state (trending/ranging/volatile), most pairs currently ranging |

### CC REST Toolkit

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/balances` | GET | Cash + portfolio value |
| `/api/rotation-tree` | GET | All positions + P&L |
| `/api/trade-outcomes?lookback_days=N` | GET | Trade history for post-mortems |
| `/api/ohlcv/{pair}?interval=60&count=50` | GET | Raw OHLCV bars |
| `/api/kronos/{pair}?interval=60&pred_len=24` | GET | Kronos 24h candle prediction |
| `/api/regime/{pair}?interval=60&count=300` | GET | HMM regime + trade_gate |
| `/api/memory?category=X&hours=N` | GET | Query CC temporal memory |
| `/api/orders` | POST | Place order (JSON body) |
| `/api/memory` | POST | Write to CC memory |
| `/api/orders/{id}` | DELETE | Cancel order |

### CC Scripts

| Script | Purpose |
|--------|---------|
| `python scripts/cc_brain.py` | Full brain cycle (analyze + decide + act) |
| `python scripts/cc_brain.py --dry-run` | Analysis only, no orders |
| `python scripts/cc_postmortem.py` | Standalone post-mortem analysis |

### Post-mortem findings (2026-04-11)

- **Win rate**: 43% (6W / 8L over 14 trades)
- **Profit factor**: 0.13 (terrible — losses far exceed wins)
- **Fee burden**: 84% of gross wins eaten by fees
- **Root trades**: -$16.19 net (22% WR) — bot was auto-selling in ranging markets
- **Child trades**: +$1.60 net (80% WR) — actual entries work when pairs are trending
- **Key insight**: Bot traded too much in ranging markets. HMM regime filter now prevents this.
- **First CC brain run**: Correctly sat out — all pairs ranging (trade_gate < 0.40)

### Known issues (2026-04-11)

- Dust positions (ASTER, AZTEC, BANANAS31) from failed bot orders now show as roots — will expire and consolidate
- `runtime_dlls/` directory cleanup safe (no longer needed)
- cp1252 encoding warnings in bot log (benign)
- AKT was force-closed by Phase 7 fix on restart

## Prediction models

| Model | Endpoint/Source | What it sees | Speed |
|-------|----------------|-------------|-------|
| RSI + EMA | OHLCV bars | Momentum + trend (1H/4H) | Instant |
| TimesFM | `beliefs/timesfm_source.py` | Close-price trajectory (21d → 24h) | ~6s |
| Kronos | `GET /api/kronos/{pair}` | Full OHLCV candle structure (400 bars → 24h) | ~4s |
| HMM Regime | `GET /api/regime/{pair}` | Market state: trending/ranging/volatile | ~200ms |

Kronos repo at `C:/Users/rober/Downloads/Projects/kronos` (cloned from shiyu-coder/Kronos).
Kronos-mini (4.1M params) on Intel Arc GPU. Tokenizer: NeoQuasar/Kronos-Tokenizer-base.

## CC Temporal Memory

SQLite table `cc_memory` in `data/bot.db`. Categories:
- `decision` — what CC decided and why (action, signals, reasoning)
- `observation` — market insights, patterns noticed
- `portfolio_snapshot` — point-in-time state captures
- `regime` — HMM results per pair over time
- `postmortem` — trade analysis summaries
- `param_change` — strategy parameter adjustments with rationale

Module: `persistence/cc_memory.py`. REST: `GET/POST /api/memory`.

## Running

```bash
# Launch bot (Windows Python, from project root)
C:\Python313\python.exe main.py

# Launch TUI (separate terminal)
C:\Python313\python.exe -m tui

# Run CC brain cycle
C:\Python313\python.exe scripts/cc_brain.py          # live
C:\Python313\python.exe scripts/cc_brain.py --dry-run # analysis only

# Run post-mortem
C:\Python313\python.exe scripts/cc_postmortem.py
```

### Key env vars (.env)

```
KRAKEN_API_KEY=...
KRAKEN_API_SECRET=...
CC_BRAIN_MODE=true
BELIEF_MODEL=timesfm
ENABLE_ROTATION_TREE=true
MTF_4H_GATE_ENABLED=true
MTF_15M_CONFIRM_ENABLED=true
MTF_15M_MAX_DEFERRALS=6
MTF_ALIGNED_BOOST=1.15
MTF_COUNTER_PENALTY=0.3
SCANNER_TIMEOUT_SEC=45
WEB_HOST=0.0.0.0
WEB_PORT=58392
READ_ONLY_EXCHANGE=false
DISABLE_ORDER_MUTATIONS=false
MIN_POSITION_USD=10
MAX_POSITION_USD=50
ROTATION_MIN_CONFIDENCE=0.65
SCANNER_MIN_24H_VOLUME_USD=50000
SCANNER_MAX_SPREAD_PCT=2.0
```

## CC Trading Philosophy

- **Simple systems win.** RSI + EMA + Kronos + HMM regime. No other indicators.
- **1% monthly target.** Anything above is bonus that reduces future risk.
- **Don't chase.** If you missed a move, wait for the next setup.
- **Regime first.** Don't trade in ranging markets (HMM trade_gate < 0.40).
- **4H trend alignment.** Only enter with the 4H trend, never against it.
- **Post-mortem everything.** Every closed trade gets analyzed. Patterns become rules.
- **Memory is continuity.** Write decisions and reasoning. Future CC reads them.

## Goal for next session

### Priority 1: Monitor and tune CC brain

- Run `scripts/cc_brain.py` a few cycles, observe decisions
- Tune scoring thresholds based on real market conditions
- Watch the AVAX/USD trade outcome (first CC-placed trade)

### Priority 2: Simplify TA ensemble

- Strip from 6 signals (EMA, RSI, MACD, Bollinger, momentum x2) to 2 (RSI + EMA)
- The bot's TA ensemble is less relevant now (CC does its own analysis) but still used by TimesFM belief handler

### Priority 3: Consolidate dust positions

- ASTER, AZTEC, BANANAS31 are small dust from failed bot orders
- Should be sold and consolidated to USD
- CC can do this via POST /api/orders

### Priority 4: Expand pair coverage

- Current TOP_PAIRS in cc_brain.py is 15 pairs — expand or auto-discover from Kraken
- Add volume-weighted pair selection

## What shipped 2026-04-11 (11 commits)

- **Phase 7**: EXPIRED auto-liquidation (AKT unstuck), stablecoin root P&L fix
- **Phase 8**: 4H trend gate + 15M entry confirmation (MTF analysis)
- **CC Command API**: POST /api/orders, DELETE /api/orders/{id}, GET /api/trade-outcomes, GET /api/ohlcv/{pair}, GET /api/balances (Codex-reviewed, all findings addressed)
- **Kronos integration**: GET /api/kronos/{pair} — candle prediction, 4.1M params on Intel Arc GPU
- **HMM regime detector**: GET /api/regime/{pair} — 3-state (trending/ranging/volatile), CC+Codex council design
- **CC temporal memory**: persistence/cc_memory.py — decisions, observations, regimes, post-mortems, param changes
- **CC post-mortem engine**: scripts/cc_postmortem.py — automated trade analysis
- **CC brain loop**: scripts/cc_brain.py — complete 8-step decision cycle
- **CC_BRAIN_MODE**: config flag disabling bot's autonomous planner (Codex-implemented)
- **First CC trade**: AVAX/USD limit buy (all signals aligned: RSI=15, 4H up, Kronos bullish)

## Validation

```bash
python -m pytest tests/ -x           # 676+ tests
python -m ruff check .               # clean (pre-existing failures in beliefs/, research/, scripts/)
curl http://127.0.0.1:58392/api/health
curl http://127.0.0.1:58392/api/balances
curl http://127.0.0.1:58392/api/regime/SOL%2FUSD
python scripts/cc_brain.py --dry-run  # verify brain cycle
```
