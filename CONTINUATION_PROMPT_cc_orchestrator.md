# CC Orchestrator -- Continuation Prompt

This file documents the **autonomous dev loop** that runs above CC-Brain.
It is appended to by `scripts/dev_loop.ps1` on every run.

## Architecture (3 layers)

```
Layer 3: CC Orchestrator (THIS layer)            <-- you are here
   scripts/dev_loop.ps1 fired by Task Scheduler every 6h
       claude --print -p (scripts/dev_loop_prompt.md)
       observes: brain reports, trade outcomes, errors, git log, Agent runner state
       acts:     writes spec/plan, dispatches Codex, verifies, commits, restarts
       budget:   1 spec/run, 320k tokens/day, max 60 turns, 20 min wall-clock

Layer 2: CC-Brain (cc_brain.py --loop)
   reads memories, observes portfolio, scores entries, places orders, writes memories
   runs every 30 min in --loop mode (PID tracked in process list)

Layer 1: Bot (main.py)
   WebSocket prices, TP/SL, fill settlement, REST API at :58392
   "deterministic body" -- no autonomous decisions when CC_BRAIN_MODE=true
```

## Files owned by Layer 3

| Path | Purpose |
|------|---------|
| `scripts/dev_loop.ps1` | Pre/post flight wrapper (PowerShell) |
| `scripts/dev_loop_prompt.md` | The prompt the LLM follows |
| `scripts/register_dev_loop_task.ps1` | Helper to register the Windows scheduled task |
| `state/dev-loop/state.json` | Persistent state across runs |
| `state/dev-loop/disabled` | Manual kill switch -- `touch` to disable |
| `state/dev-loop/escalate.md` | Written by loop when stuck -- user must resolve |
| `state/dev-loop/runs/<ts>.log` | Per-run claude stdout/stderr |
| `state/dev-loop/runs/<ts>.summary.md` | Per-run structured summary |
| `CONTINUATION_PROMPT_cc_orchestrator.md` | THIS file -- chronological run log + current state |

## Hard rules (the loop will NOT violate these)

1. MAX 1 spec dispatched per run
2. NEVER push to remote
3. NEVER edit code itself (always dispatch to Codex via cross-agent.py)
4. NEVER modify env / `.env` / `CC_BRAIN_MODE`
5. NEVER restart `main.py` if uptime < 1h
6. NEVER dispatch with the same slug as the previous run
7. NEVER dispatch if previous spec is "unsettled" (1+ brain cycle since commit AND no new permission_blocked / stuck_dust)
8. NEVER touch `tasks/lessons.md` or `CLAUDE.md`
9. ALWAYS update this file at end of every run
10. If anything risky / unclear -> write `escalate.md` and exit

## Pre-flight gates (the wrapper will skip the run if any fail)

| Gate | Failure mode |
|------|--------------|
| `state/dev-loop/disabled` exists | Manual kill -- user disabled |
| `state/dev-loop/escalate.md` exists | Loop is waiting for user resolution |
| `consecutive_failures >= 3` | Auto-disabled, requires manual reset |
| Bot `/api/health` unreachable | Bot is down -- escalate manually |
| Bot uptime < 3600s | Bot just restarted, let it settle |
| Last commit < 30 min old | Previous spec hasn't settled yet |
| Unstaged user changes (modified, not untracked) | User is mid-edit, don't stomp |
| Daily token budget exceeded (320k) | Cost cap |

## How to disable / re-enable

```powershell
# Disable the next run
New-Item -Path state/dev-loop/disabled -ItemType File -Force

# Re-enable
Remove-Item state/dev-loop/disabled

# Reset failure counter (after fixing whatever escalated)
# Edit state/dev-loop/state.json and set consecutive_failures = 0
# Then delete state/dev-loop/escalate.md
```

## How to manually fire (for testing)

```powershell
# Normal run (gates enforced)
pwsh -File scripts/dev_loop.ps1

# Dry run -- gates enforced, claude invoked, but no commit/restart (relies on prompt to honor flag)
pwsh -File scripts/dev_loop.ps1 -DryRun

# Force -- bypass gates (use sparingly, e.g. to test after fixing escalation)
pwsh -File scripts/dev_loop.ps1 -Force
```

## How to register the scheduled task

```powershell
pwsh -File scripts/register_dev_loop_task.ps1
```

This registers `KrakenBot-CcOrchestrator` to fire every 6 hours offset from the brain cycle (which fires every 2h).

## Current state (snapshot at activation)

For live state, read `state/dev-loop/state.json` and the most recent file in `state/dev-loop/runs/`. The wrapper updates state.json on every fire; the run log file has the full claude reasoning trace.

- **Loop status**: bring-up complete, scheduler registration pending
- **Total runs at handoff**: 2 (1 dry, 1 live no-action)
- **Bot version at activation**: 688 tests passing, specs 1-13 all live, master at `96f4a31`

## Run log

Each run appends one line below. Format: `- <UTC ts> -- **<status>** action=<...> spec=<...> commit=<sha> restarted=<...>`. The wrapper owns this section -- claude is told NOT to write here directly.

### Bring-up (2026-04-12)

- 20260412_182308 UTC -- **dry_run** action=would_dispatch (proposed spec=14 seed-restricted-fiat-pair-blacklist) -- bring-up dry run, identified historical AUD/USD recurring pattern from pre-restart cycles
- 20260412_182639 UTC -- **error** (claude refused: curl tool not allowed under acceptEdits permission mode) -- fixed by switching wrapper to bypassPermissions
- 20260412_182741 UTC -- **no_action** -- first successful live fire (222s wall-clock). Correctly deferred: AUD/USD pattern no longer present after spec 12 went live, USDT phantom already fixed by spec 11, recon warning is benign held-fiat accounting. Run log: `state/dev-loop/runs/20260412_182741.log`

### Scheduled fires

- 20260412_185432 UTC -- **dry_run** action=would_dispatch spec=blacklist-restricted-fiat-pairs

- 20260412_185827 UTC -- **dry_run** action=no_action

- 20260412_191500 UTC -- **skipped** (bot uptime < 1h (87s))

- 20260412_202856 UTC -- **dry_run** action=no_action

- 20260412_210639 UTC -- **no_action**

- 20260412_211032 UTC -- **dry_run** action=no_action

- 20260412_212147 UTC -- **dry_run** action=no_action

- 20260412_213426 UTC -- **skipped** (daily token budget exceeded (1599207 tokens))

- 20260412_213454 UTC -- **dry_run** action=no_action

- 20260413_011501 UTC -- **skipped** (bot uptime < 1h (2330s))

- 20260413_071502 UTC -- **skipped** (unstaged user changes present (user is mid-edit))

- 20260413_131502 UTC -- **skipped** (unstaged user changes present (user is mid-edit))

- 20260413_191502 UTC -- **skipped** (unstaged user changes present (user is mid-edit))

- 20260414_011502 UTC -- **skipped** (bot uptime < 1h (1192s))

- 20260414_071502 UTC -- **completed** action=spec_dispatched spec=widen-recon-dedupe-windows commit=38764d9 restarted=main
