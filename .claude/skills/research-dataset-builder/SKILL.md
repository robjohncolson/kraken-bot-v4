---
name: research-dataset-builder
description: "Use when resuming Phase 1 of the autoresearch roadmap in kraken-bot-v4: repair the build cross-agent harness if needed, create a research-dataset phase manifest, and drive offline dataset export through the existing build workflow."
---

# Research Dataset Builder

Repo-specific workflow for building the Phase 1 research dataset export in `kraken-bot-v4`.

This skill is intentionally a stub. Claude should refine it as the workflow stabilizes.

## Goal

Build the offline dataset export path described in:
- `CONTINUATION_PROMPT.md`
- `docs/specs/autoresearch-trading-research-spec.md`
- `docs/specs/autoresearch-trading-implementation-checklist.md`

Target outputs:
- `data/research/market_v1.parquet`
- `data/research/labels_v1.parquet`
- `data/research/manifest_v1.json`

## Read First

1. `CONTINUATION_PROMPT.md`
2. `docs/specs/autoresearch-trading-research-spec.md`
3. `docs/specs/autoresearch-trading-implementation-checklist.md`
4. `build/BUILD_SYSTEM_DESIGN.md`
5. `build/common.py`
6. `build/run_phase.py`
7. `AGENTS.md`
8. `CLAUDE.md`

## First Check: Harness Bootstrap

Verify the cross-agent runner path before trying to execute the build harness.

Known machine-local expectation:
- actual runner path is likely `C:\Users\rober\Downloads\Projects\Agent\runner\cross-agent.py`

Known repo issue:
- `build/common.py` may still point at a user-specific `C:\Users\ColsonR\...` path

Preferred fix:
- make the runner path configurable
- keep a safe local fallback
- avoid scattering hardcoded machine-specific paths

## Build Workflow

1. Repair and verify the harness.
2. Create a dedicated manifest for the dataset-export phase.
3. Keep tasks atomic and small.
4. Use the harness, not ad hoc delegation, once bootstrap is fixed.
5. Run targeted tests per task.
6. Commit one task at a time, locally only.

## Manifest Guidance

Suggested manifest path:
- `build/manifests/phase-10.research-dataset.json`

Suggested task shape:
- `10.1` harness bootstrap
- `10.2` dataset schema + manifest writer
- `10.3` OHLCV extraction
- `10.4` SQLite extraction
- `10.5` label generation
- `10.6` exporter CLI + deterministic tests

Each task should have:
- narrow `owned_paths`
- explicit `acceptance_criteria`
- explicit `test_targets`
- no more than 10 touched files

## Hard Constraints

- No future data in features.
- Deterministic output for a fixed input snapshot.
- Keep live trading runtime changes out of scope unless required for safe reuse.
- Do not push.

## Verification

Minimum:
- targeted `pytest` for each task
- `python -m pytest -q`
- `python -m ruff check .`

Also:
- use GitNexus impact before editing symbols
- run `gitnexus_detect_changes()` before commits

## Deliverables

- harness bootstrap fix
- phase manifest
- dataset export implementation
- tests
- minimal run documentation

## TODO For Claude

- tighten the exact task decomposition once Phase 1 implementation starts
- document the final harness bootstrap approach
- add the exact exporter command once implemented
- add common failure cases and recovery steps
