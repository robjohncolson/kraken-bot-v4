# CC Orchestrator — Continuation Prompt

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
   "deterministic body" — no autonomous decisions when CC_BRAIN_MODE=true
```

## Files owned by Layer 3

| Path | Purpose |
|------|---------|
| `scripts/dev_loop.ps1` | Pre/post flight wrapper (PowerShell) |
| `scripts/dev_loop_prompt.md` | The prompt the LLM follows |
| `scripts/register_dev_loop_task.ps1` | Helper to register the Windows scheduled task |
| `state/dev-loop/state.json` | Persistent state across runs |
| `state/dev-loop/disabled` | Manual kill switch — `touch` to disable |
| `state/dev-loop/escalate.md` | Written by loop when stuck — user must resolve |
| `state/dev-loop/runs/<ts>.log` | Per-run claude stdout/stderr |
| `state/dev-loop/runs/<ts>.summary.md` | Per-run structured summary |
| `CONTINUATION_PROMPT_cc_orchestrator.md` | THIS file — chronological run log + current state |

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
10. If anything risky / unclear → write `escalate.md` and exit

## Pre-flight gates (the wrapper will skip the run if any fail)

| Gate | Failure mode |
|------|--------------|
| `state/dev-loop/disabled` exists | Manual kill — user disabled |
| `state/dev-loop/escalate.md` exists | Loop is waiting for user resolution |
| `consecutive_failures >= 3` | Auto-disabled, requires manual reset |
| Bot `/api/health` unreachable | Bot is down — escalate manually |
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

# Dry run — gates enforced, claude invoked, but no commit/restart (relies on prompt to honor flag)
pwsh -File scripts/dev_loop.ps1 -DryRun

# Force — bypass gates (use sparingly, e.g. to test after fixing escalation)
pwsh -File scripts/dev_loop.ps1 -Force
```

## How to register the scheduled task

```powershell
pwsh -File scripts/register_dev_loop_task.ps1
```

This registers `KrakenBot-CcOrchestrator` to fire every 6 hours offset from the brain cycle (which fires every 2h).

## Current state

- **Loop status**: not yet activated. Files exist but task is not registered.
- **Total runs**: 0
- **Total specs dispatched by loop**: 0
- **Last action**: init
- **Last commit (manual session 4)**: `b498d48` — docs(continuation): session 4 — specs 11/12/13 landed
- **Bot version at activation**: 688 tests passing, specs 1-13 all live, master branch

## Run log

(Each run appends one line below. Format: `- <UTC ts> — **<status>** action=<...> spec=<...> commit=<sha> restarted=<...>`)

