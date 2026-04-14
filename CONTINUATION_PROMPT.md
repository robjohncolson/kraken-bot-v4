# Continuation Prompt — kraken-bot-v4

> Slim handoff. Anything that can be recovered from `git log`, `tasks/specs/`,
> or by reading code is intentionally omitted. Use those tools when you need
> history.

## Architecture (3 layers)

```
Layer 3: CC Orchestrator
  Windows Task Scheduler "KrakenBot-CcOrchestrator" fires every 6h.
  scripts/dev_loop.ps1 -> claude --print with scripts/dev_loop_prompt.md
  Observes layers 1+2, picks the highest-leverage issue, dispatches Codex,
  verifies, commits, restarts. MAX 1 spec/run, never pushes.
  Wrapper-owned run log: CONTINUATION_PROMPT_cc_orchestrator.md

Layer 2: CC Brain (scripts/cc_brain.py --loop, also scheduled every 2h)
  Reads cc_memory -> observes portfolio -> analyzes pairs
  (HMM regime + RSI/EMA + Kronos + TimesFM) -> scores entries ->
  places orders or sits out -> writes decisions back to cc_memory.

Layer 1: Bot (main.py, always-on)
  WebSocket prices, TP/SL/trailing monitoring, fill settlement,
  REST API on :58392. CC_BRAIN_MODE=true disables its own planner --
  the bot is a deterministic body, CC is the brain.
```

- **Host**: spare laptop at home, always on
- **Exchange**: Kraken (Starter tier, US:MA -- some pairs blacklisted)
- **Persistence**: SQLite `data/bot.db` (WAL mode)
- **Platform**: Windows 11, Python 3.13, Intel Arc GPU (torch 2.8+xpu)
- **Repo**: `git@github.com:robjohncolson/kraken-bot-v4.git`, branch `master`

## Running

```bash
C:\Python313\python.exe main.py                     # bot (always-on)
C:\Python313\python.exe -m tui                      # operator cockpit
C:\Python313\python.exe scripts/cc_brain.py         # one-shot brain cycle
C:\Python313\python.exe scripts/cc_brain.py --loop  # brain daemon
C:\Python313\python.exe scripts/cc_postmortem.py    # trade analysis (also runs premature-exit detector)
C:\Python313\python.exe analysis/premature_exit.py --lookback-days 30 [--dry-run]
```

Key env vars in `.env`: `CC_BRAIN_MODE=true`, `BELIEF_MODEL=timesfm`,
`WEB_HOST=0.0.0.0`, `WEB_PORT=58392`, `MAX_POSITION_USD=50`,
`MIN_POSITION_USD=10`, `ROTATION_MIN_CONFIDENCE=0.65`. See `.env` for full set.

## Orchestrator (Layer 3)

| Component | Path |
|-----------|------|
| Wrapper | `scripts/dev_loop.ps1` |
| Prompt | `scripts/dev_loop_prompt.md` |
| Pre-flight gates | `scripts/dev_loop.ps1` (PowerShell, deterministic) |
| State | `state/dev-loop/state.json` |
| Per-run logs | `state/dev-loop/runs/<ts>.{log,summary.md}` |
| Rolling run log | `CONTINUATION_PROMPT_cc_orchestrator.md` |
| Escalation | `state/dev-loop/escalate.md` |
| Manual disable | `New-Item state/dev-loop/disabled -ItemType File` |
| Manual fire | `powershell -File scripts/dev_loop.ps1 [-Force] [-DryRun]` |
| Unregister | `powershell -File scripts/register_dev_loop_task.ps1 -Unregister` |

Read `scripts/dev_loop_prompt.md` for the hard rules (1-spec/run cap, no
push, no env edits, no `tasks/lessons.md` writes, etc.) before touching
orchestrator behavior. Pre-flight gates (uptime, commit age, token budget,
unsettled spec) live in `scripts/dev_loop.ps1`.

## REST Toolkit

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/health` | GET | Bot liveness + uptime |
| `/api/balances` | GET | Cash + portfolio value |
| `/api/rotation-tree` | GET | All positions + P&L |
| `/api/open-orders` | GET | Live exchange order state |
| `/api/trade-outcomes?lookback_days=N` | GET | Closed trades |
| `/api/ohlcv/{pair}?interval=60&count=50` | GET | OHLCV bars |
| `/api/kronos/{pair}` | GET | Kronos candle prediction |
| `/api/regime/{pair}` | GET | HMM regime + trade_gate |
| `/api/timesfm/{pair}` | GET | TimesFM trajectory |
| `/api/memory?category=X&hours=N` | GET | Query CC memory |
| `/api/orders` | POST | Place order |
| `/api/orders/{id}` | DELETE | Cancel order |
| `/api/memory` | POST | Write CC memory |

Source of truth: `web/routes.py`.

## Prediction models

| Model | Source | Timeframe |
|-------|--------|-----------|
| RSI(14) + EMA(7/26) | `scripts/cc_brain.py` (1H + 4H) | momentum + trend |
| TimesFM | `beliefs/timesfm_source.py` | close-price trajectory |
| Kronos-mini (4.1M params) | `C:/Users/rober/Downloads/Projects/kronos` | full OHLCV candle |
| HMM regime (3-state) | `trading/regime_detector.py` | trending/ranging/volatile |

## CC Memory categories (`persistence/cc_memory.py`)

`decision`, `observation`, `portfolio_snapshot`, `regime`, `postmortem`,
`param_change`, `shadow_verdict`, `permission_blocked`, `stuck_dust`,
`reconciliation_anomaly`, `rotation_tree_drift`, `pending_order`,
`premature_exit`.

## Open follow-ups (orchestrator-eligible)

1. **Premature exit forward data**: spec 34 detector is live. Once
   `cc_memory(category=premature_exit)` shows >=5 entries in 14d of
   forward-going data, add a 1-line trigger to `scripts/dev_loop_prompt.md`
   so the orchestrator can spec a shadow-mode EMA-trail exit policy.
2. **Token tracking in `dev_loop.ps1`**: budget gate exists but doesn't
   parse claude's token output yet.
3. **Weekly review prompt**: separate cadence, only after the 6h tactical
   loop has been stable for a few days.
4. **`untracked_assets` reconciliation noise**: orchestrator has been
   correctly deferring as benign held-fiat accounting. Worth a manual look.

## Trading philosophy

- Simple systems win. RSI + EMA + Kronos + HMM regime. Nothing else.
- 1% monthly target. Anything above is bonus that reduces future risk.
- Regime first -- don't trade in ranging markets (trade_gate < 0.40).
- 4H trend alignment -- never enter against the 4H trend.
- Limit orders only (never market unless risk demands it).
- Currency-agnostic -- USD is not "cash"; stability != fiat status.
- TA-driven exits, not USD-anchored exits.
- Memory is continuity -- write decisions and reasoning.

## Pointers

- **Spec history**: `tasks/specs/NN-slug.{spec,plan,result}.md`
- **Lessons learned**: `tasks/lessons.md`
- **GitNexus impact analysis**: see top-level `CLAUDE.md`
- **Cross-agent dispatch**:
  ```
  python ../Agent/runner/cross-agent.py \
    --direction cc-to-codex --task-type implement \
    --working-dir <repo> --owned-paths <files> \
    --timeout 1200 --prompt "..."
  ```

## Validation smoke tests

```bash
C:/Python313/python.exe -m pytest tests/ -x -q
curl http://127.0.0.1:58392/api/health
curl http://127.0.0.1:58392/api/balances
C:/Python313/python.exe scripts/cc_brain.py --dry-run
```
