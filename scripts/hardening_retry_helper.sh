#!/bin/bash
# Post-batch retry helper for the kraken-bot-hardening dispatch.
#
# Run after the parallel-codex-runner completes (success or partial).
# Operations:
#   1. List the codex/* branches and their commit status
#   2. Merge any completed agent branches to master (skip if already merged)
#   3. Clean orphaned worktree directories
#   4. Print the set of agents that still need retrying
#
# Usage: bash scripts/hardening_retry_helper.sh

set -e
cd "$(git rev-parse --show-toplevel)"

echo "=== Current master HEAD ==="
git log --oneline -1

echo ""
echo "=== Codex branches (vs master) ==="
for br in $(git branch | grep -E "^\s*codex/" | tr -d ' '); do
    ahead=$(git rev-list --count master.."$br" 2>/dev/null || echo "?")
    behind=$(git rev-list --count "$br"..master 2>/dev/null || echo "?")
    last=$(git log --format="%h %s" -1 "$br" 2>/dev/null | head -1)
    echo "  $br  ahead=$ahead behind=$behind  $last"
done

echo ""
echo "=== Orphaned worktree directories (not registered) ==="
registered=$(git worktree list --porcelain | grep "^worktree " | cut -d' ' -f2)
if [ -d state/parallel-worktrees ]; then
    for d in state/parallel-worktrees/*/; do
        [ -d "$d" ] || continue
        abs=$(cd "$d" && pwd -W 2>/dev/null || cd "$d" && pwd)
        if ! echo "$registered" | grep -qF "$abs"; then
            echo "  ORPHAN: $d"
        fi
    done
fi

echo ""
echo "=== Agents in parallel-batch.json state ==="
python -c "
import json
s = json.load(open('state/parallel-batch.json'))
print(f'  Status: {s[\"status\"]}')
for a in s.get('agents', []):
    st = a['status']
    err = (a.get('error') or {}).get('message', '')[:70]
    print(f'  {a[\"name\"]:25s} {st:10s} {err}')
"

echo ""
echo "=== Suggested next steps ==="
echo "  1. For each codex/<agent> branch with ahead>0 and commit not already on master:"
echo "       git merge --no-ff codex/<agent> -m 'Merge codex/<agent>: ...'"
echo "  2. Delete orphaned worktree directories:"
echo "       rm -rf state/parallel-worktrees/<agent>"
echo "  3. (Optional) delete merged branches:"
echo "       git branch -d codex/<agent>"
echo "  4. Re-dispatch remaining agents with a trimmed manifest or run:"
echo "       python ../Agent/runner/parallel-codex-runner.py --manifest dispatch/kraken-bot-hardening.manifest.json --reset"
