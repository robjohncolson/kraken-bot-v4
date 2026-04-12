# CONTINUATION_PROMPT_CODEX.md

This is the handoff doc for the **autonomous CC+Codex session loop** described in `CLAUDE.md`. A fresh CC session reading this file should be able to resume the work without re-explaining the workflow.

The user's standing instruction: keep going until `/context` reads above 70%, then stop. Update this doc at every pause. Use cross-agent dispatch to Codex for all implementation.

## Where the loop currently is

**As of 2026-04-12 session 4 part 3** — building introspection improvements for the Layer 3 CC Orchestrator (`scripts/dev_loop.ps1`). Two specs in flight:

- **Spec 17 — time-window the orchestrator's observation step**. Wrapper precomputes `last_code_commit_ts` (most recent commit touching .py files, not docs/state). Prompt instructs claude: "when counting recurring patterns from brain reports / memories, only count those with timestamp > LAST_CODE_COMMIT_TS." Kills the dominant false-positive class (LLM reading pre-fix brain reports as current pathology).

- **Spec 18 — Codex challenge on no_action verdicts**. After the main claude run, if status=no_action OR response contains "benign"/"deferred", wrapper fires a second `cross-agent.py investigate` dispatch asking Codex to verify or refute the verdict on the specific finding. If Codex disagrees with evidence, write to `state/dev-loop/escalate.md`. Doubles token cost on no_action runs but caught the untracked_assets bug today and would catch future ones.

Both specs follow from a 2026-04-12 18:30 UTC user observation: "the orchestrator was wrong twice in a row" — once on AUD/USD recurrence (counted pre-fix cycles), once on untracked_assets (called it benign without checking SQLite).

## Specs already landed in session 4 (in chronological order)

| # | Slug | Status | Notes |
|---|------|--------|-------|
| 11 | runtime-loop-root-exit-unit-fix | LIVE | USDT phantom $15.85 fixed via quote-side root exit accounting |
| 12 | permissions-blacklist | LIVE | AUD/USD permission errors now blacklisted after first failure |
| 13 | parallel-runner-stale-worktree-cleanup | LIVE (Agent repo) | `_force_remove_dir` helper replaces silent `ignore_errors=True` |
| 14 | dev-loop-token-tracking | LIVE | Wrapper now uses `--output-format json`, sums uncached + cache_create + cache_read for full input footprint |
| 15 | untracked-assets-investigation | INVESTIGATION ONLY | Found CC `/api/orders` placements bypass SQLite tracking |
| 16 | persist-cc-api-orders | LIVE | `place_order()` now calls `upsert_order` with `kind='cc_api'` |
| 17 | dev-loop-time-window-observation | TODO | Fix A from the user discussion |
| 18 | dev-loop-codex-challenge-verdicts | TODO | Fix B from the user discussion |

Tests at last green: **690 passing** (679 baseline + 11 new across specs 11-16).

## Architecture (3 layers, all live as of session 4)

```
Layer 3: CC Orchestrator      KrakenBot-CcOrchestrator scheduled task, every 6h
                              scripts/dev_loop.ps1 -> claude --print
                              State at state/dev-loop/state.json
                              Run logs at state/dev-loop/runs/<ts>.log
                              Doc: CONTINUATION_PROMPT_cc_orchestrator.md

Layer 2: CC-Brain             scripts/cc_brain.py --loop (PID tracked in process list)
                              Reads memories, scores entries, places orders, writes verdicts

Layer 1: Bot                  main.py (PID tracked in process list)
                              WebSocket prices, TP/SL, fills, REST API at :58392
```

## Standing rules (do not violate without user say-so)

- NEVER push to remote without an explicit user push in the current turn
- NEVER edit code yourself — always dispatch to Codex via cross-agent.py
- NEVER modify `.env` / `CC_BRAIN_MODE` / `CLAUDE.md` / `tasks/lessons.md`
- NEVER restart `main.py` if `/api/health` uptime < 3600s
- NEVER dispatch with the same slug as the previous run
- ALWAYS verify pytest is green before committing Codex's work
- ALWAYS update this file at pause points
- If something is unclear → stop and ask, do not guess

## Cross-agent dispatch invocation pattern

```bash
C:/Python313/python.exe /c/Users/rober/Downloads/Projects/Agent/runner/cross-agent.py \
  --direction cc-to-codex \
  --task-type implement \
  --working-dir "C:/Users/rober/Downloads/Projects/<repo>" \
  --owned-paths "path1" "path2" "tasks/specs/NN-slug.result.md" \
  --timeout 1200 \
  --prompt "<implementation prompt referencing the spec+plan files>" \
  2>&1 | tee state/codex-specNN.log | tail -30
```

Always include the result file path in `owned_paths` so Codex can write its summary. Codex does NOT run tests in subagent mode — verify yourself with `python -m pytest tests/ -x`.

## Run log (this loop)

Each entry is a paragraph of context that survives a clean session restart. Format:
- timestamp UTC
- what was attempted
- what landed (commit hash + brief)
- context % at end (or estimate if /context wasn't run)
- next target

### 2026-04-12T19:00Z — initial handoff doc created

Loop just transitioned from manual user dispatch to autonomous mode. User said "go for it" on specs 17 + 18 (orchestrator self-correction). Context at start of this batch: ~35% (estimated, /context last read 29% before specs 14/15/16 landed). About to dispatch spec 17.

Next target: spec 17 implementation via Codex dispatch.
