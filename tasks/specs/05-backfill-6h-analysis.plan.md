# Plan 05 — Backfill 6h analysis

## Nature of task

**Diagnostic**, not implementation. No code to write unless the
existing backfill script has a bug preventing 6h evaluation. If
it runs cleanly, produce a markdown analysis.

## File targets

- `scripts/backfill_shadow.py` — READ ONLY (unless bug found)
- `tasks/specs/05-backfill-6h-analysis.result.md` — NEW, the
  written analysis
- `state/cc-reviews/brain_*.md` — READ ONLY input data

## Step-by-step

### Step 1: Confirm data availability

```bash
ls state/cc-reviews/brain_2026-04-*.md | wc -l
python -c "
from pathlib import Path
from datetime import datetime, timezone
import time
now = time.time()
reports = sorted(Path('state/cc-reviews').glob('brain_2026-04-*.md'))
usable = 0
for p in reports:
    stem = p.stem
    _, d, t = stem.split('_', 2)
    dt = datetime.strptime(f'{d}_{t}', '%Y-%m-%d_%H%M').replace(tzinfo=timezone.utc)
    age_h = (now - dt.timestamp()) / 3600
    if age_h > 7:
        usable += 1
print(f'Reports > 7h old (evaluable at 6h window): {usable}')
"
```

Fail early if usable < 8.

### Step 2: Run the backfill

```bash
python scripts/backfill_shadow.py --forward-hours 6 > /tmp/backfill_6h.txt 2>&1
```

Capture full stdout to a file.

### Step 3: Validate sanity check

First section of output must show:
`score_entry recovery: N/N (100%) within 0.05 of logged score`

If anything less than 100%, STOP and report the discrepancy as
a bug. Reconstruction fidelity is the foundation — bad numbers
here invalidate the analysis.

### Step 4: Extract key metrics

From the output, note:
- Total cycles analyzed
- Cycles with sufficient forward data
- Live decision type distribution
- Shadow best-hold picks distribution
- Agreement rate on order cycles
- Top disagreement patterns
- Eligibility coverage per asset
- Per-cycle forward-return table

### Step 5: Compute derived statistics

Given the per-cycle table:
- Shadow-win count and rate
- Live-win count and rate
- Tie count
- Cumulative `sum(shadow_ret - live_ret)`
- Average per-cycle edge
- If possible, a simple binomial significance test:
  `from scipy.stats import binomtest; binomtest(wins, n, 0.5).pvalue`
  OR a manual calculation (one-sided test p-value):
  ```python
  # Without scipy: approximate with normal distribution for N >= 8
  from math import sqrt
  n, k = 11, 11
  z = (k - n/2) / sqrt(n/4)
  # z > 1.645 = 95% one-sided, z > 2.33 = 99% one-sided
  ```
  Only report a p-value if the sample size warrants it.

### Step 6: Write the analysis

Create `tasks/specs/05-backfill-6h-analysis.result.md` with:

```markdown
# Backfill 6h analysis — result

## Run metadata
- Command: `python scripts/backfill_shadow.py --forward-hours 6`
- Date: <UTC timestamp>
- Reports processed: N
- Reconstruction fidelity: N/N (should be 100%)
- Cycles with 6h forward data: N

## Raw per-cycle table
<copy the table from backfill output>

## Aggregate metrics
| Metric | Value |
|--------|-------|
| Shadow wins | N (P%) |
| Live wins   | N (P%) |
| Ties        | N (P%) |
| Cumulative edge (shadow - live) | ±X.XX% |
| Avg per-cycle edge | ±X.XX% |
| Binomial 1-sided p (if n >= 10) | p=X.XX |

## Comparison to 2h window result
- 2h window: 6/6 shadow, cumulative -17.80% live, clustered
- 6h window: <numbers>
- Does the directional signal hold over the longer window?

## Sample-size assessment
- How many independent decisions? (After deduping cases where the
  same pair was retried across consecutive cycles, which the
  pending-order fix should have largely eliminated)
- Is the window broad enough to capture multiple market regimes?

## Disagreement pattern
<list top 5 disagreements with forward returns>

## Recommendation
One of:
1. **Promote** — shadow replaces live entry logic entirely
2. **Extend veto** — proceed with spec 04 (hybrid redirect/veto)
3. **Hold narrow** — keep USD-only veto, collect more data
4. **Investigate** — there's a bias or bug, do not change behavior

## Follow-up specs (if any)
<list any new specs this analysis suggests>
```

## Testing

The task is itself a test. If the analysis file is produced with
all sections filled in and numbers match the backfill output, it's
done.

## Rollback

None needed — this task only reads code and writes a markdown file
in `tasks/specs/`. To undo, delete the result file.

## No commit message template

This task should produce a commit like:
```
Backfill 6h forward-return analysis

See tasks/specs/05-backfill-6h-analysis.result.md for the full
write-up.

Key finding: <one sentence summary>
Recommendation: <one of the four options>
```
