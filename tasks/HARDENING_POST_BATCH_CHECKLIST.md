# Post-batch checklist for kraken-bot-hardening dispatch

Session 3 dispatch kicked off 2026-04-12 ~15:33 UTC. When the background
task notification fires (background task id `bxo6edv8i`), run through this
list in order.

## Step 1 — Classify final agent states

```bash
python -c "
import json
s = json.load(open('state/parallel-batch.json'))
print(f'Batch status: {s[\"status\"]}')
for a in s.get('agents', []):
    print(f'  {a[\"name\"]:25s} {a[\"status\"]:10s} attempts={a[\"attempts\"]}')
"
```

Expected per pre-batch analysis:
- `02-open-orders` → failed (worktree collision from prior run, cleaned but dispatch already marked failed)
- `06-backfill-fidelity` → likely completed (independent file, low risk)
- `09-usdt-investigation` → likely completed (diagnostic-first, may touch few files)
- `07/03/08/10` → blocked (chain depends on 02)

## Step 2 — Review and merge successful branches

For each agent with `status=completed`:

```bash
git diff --stat master..codex/<agent>
git --no-pager show codex/<agent> | head -80  # sanity check
git merge --no-ff codex/<agent> -m "Merge codex/<agent>: <short description>"
```

Expected merges (if agents succeed):
- `codex/06-backfill-fidelity` → `Merge codex/06-backfill-fidelity: filter dry-runs + failed orders from backfill`
- `codex/09-usdt-investigation` → `Merge codex/09-usdt-investigation: <finding summary from result file>`

## Step 3 — Clean up stale branches and worktrees

```bash
# Delete merged branches
git branch -d codex/01-floor-round codex/05-backfill-analysis

# Reset any remaining stale branches that have no new commits
git branch -D codex/02-open-orders  # force delete if it has no new work

# Remove orphaned worktree directories
rm -rf state/parallel-worktrees/02-open-orders
# Verify no other orphans
bash scripts/hardening_retry_helper.sh
```

## Step 4 — Retry dispatch for remaining agents

Trim the manifest or reset state and re-dispatch. Simplest:

```bash
# Remove agents already merged to master from the manifest, then re-dispatch.
# Alternatively: edit dispatch/kraken-bot-hardening.manifest.json to include
# only 02-open-orders, 07-ordermin-precheck, 03-fiat-filter, 08-maker-fee,
# 10-self-tune-fix (drop 06 and 09 if completed).
python ../Agent/runner/parallel-codex-runner.py \
    --manifest dispatch/kraken-bot-hardening.manifest.json \
    --reset \
    --max-parallel 2
```

## Step 5 — Run post-batch verification

Once all agents have merged:

```bash
python scripts/verify_hardening_batch.py
```

Expected: `10/10 checks passed`. Any FAIL indicates a spec whose
acceptance criteria weren't met.

## Step 6 — Run a live cc_brain cycle

Verify end-to-end that:
1. No `EOrder:Insufficient funds` on CRV/COMP (spec 01 held + 07 ordermin guard)
2. No `EAccount:Invalid permissions` on AUD/USD — shouldn't even be proposed (spec 03)
3. Maker fee observable on the next fill — look at `/api/trade-outcomes` after 2-3 cycles

```bash
python scripts/cc_brain.py  # live, not --dry-run
```

## Step 7 — Check accumulated shadow data

```bash
python scripts/analyze_shadow.py --hours 24
python scripts/backfill_shadow.py --forward-hours 6  # with 06 fix applied
```

The corrected backfill should now drop the RAVE/USD 01:32 cycle from
live's column and show shadow winning on the filled cycles only.

## Step 8 — Update CONTINUATION_PROMPT.md with final state

Mark which agents merged, which were dropped, what the post-batch
verification showed, and what follow-up specs (if any) emerged.

## Known follow-up specs (NOT in this batch)

- **11 — permissions-aware pair blacklist**: when Kraken returns
  `EAccount:Invalid permissions`, cache the pair as untradeable in
  memory (or a session-local set) and skip it permanently. Would have
  caught AUD/USD if the bot had been scoring it against a real */USD
  pair instead of relying on the fiat filter.
- **12 — stale worktree cleanup in parallel-runner**: the Agent
  repo's parallel-codex-runner.py has a bug where orphaned worktree
  dirs from failed runs block subsequent dispatches. Fix upstream
  in the Agent repo (or add a pre-flight cleanup step).
