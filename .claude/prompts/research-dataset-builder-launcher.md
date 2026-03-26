# Research Dataset Builder Launcher

You are Claude Code working in `C:\Users\rober\Downloads\Projects\kraken-bot-v4`.

Use the repo's existing build cross-agent harness to implement **Phase 1: Research Dataset Export**.

Read first:
- `CONTINUATION_PROMPT.md`
- `docs/specs/autoresearch-trading-research-spec.md`
- `docs/specs/autoresearch-trading-implementation-checklist.md`
- `build/BUILD_SYSTEM_DESIGN.md`
- `build/common.py`
- `build/run_phase.py`
- `.claude/skills/research-dataset-builder/SKILL.md`
- `AGENTS.md`
- `CLAUDE.md`

Immediate bootstrap issue:
- `build/common.py` points `AGENT_RUNNER` at `C:\Users\ColsonR\Agent\runner\cross-agent.py`
- that path is wrong on this machine
- actual runner path is `C:\Users\rober\Downloads\Projects\Agent\runner\cross-agent.py`
- repair the harness first, in the smallest safe way, preferably with a configurable path plus fallback

Mission:
- create a new phase manifest for Phase 1 dataset export
- run that phase through the harness
- keep scope to offline dataset export plus harness bootstrap
- do not drift into walk-forward evaluation or live model integration
- do not push

Required outputs:
- `data/research/market_v1.parquet`
- `data/research/labels_v1.parquet`
- `data/research/manifest_v1.json`

Required labels:
- `return_sign_6h`
- `return_sign_12h`
- `return_bps_6h`
- `return_bps_12h`
- `regime_label`

Required sources:
- Kraken OHLCV history
- local SQLite orders
- local SQLite fills
- local SQLite closed-trade outcomes

Hard constraints:
- no feature may use future data
- dataset generation must be deterministic for a fixed input snapshot
- use GitNexus impact before editing any function/class/method symbol
- run `gitnexus_detect_changes()` before each commit
- one atomic commit per task
- full `pytest` and `ruff` green at the end

Suggested sequence:
1. Repair and verify the harness.
2. Create `build/manifests/phase-10.research-dataset.json`.
3. Decompose into small tasks with clear `owned_paths` and `test_targets`.
4. Dry-run the phase.
5. Execute through the harness.
6. Create or refine the repo-local skill if it helps future sessions.
7. Finish with full verification and a clean summary.

Final report:
- harness fix
- manifest created
- skill created/refined
- files changed
- tests run
- commits created
- residual risks
