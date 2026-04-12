# Spec 09 — Investigate the USDT/USD −$15.85 loss

## Problem

Over the last 7 days, the bot lost $14.58 net. A single trade accounts
for almost the entire loss:

| Field | Value |
|-------|-------|
| Pair | USDT/USD |
| Entry cost | $36.96 |
| Exit proceeds | $21.11 |
| Fees | $0.04 |
| Net P&L | **−$15.85** |
| Exit reason | `root_exit_bearish` |

This is a 42.9% loss on a stablecoin pair. USDT did not depeg 43%
during this window — the actual USDT/USD price stayed within
$0.9998–$1.0002 across the entire period.

Without this one trade, the bot's underlying performance is essentially
break-even. Finding and fixing whatever caused this single trade is
probably the single highest-leverage P&L fix available — it converts
"down 3% in a week" to "flat in a week" without touching any other
behavior.

## Hypotheses

1. **Partial-fill accounting error**: the bot thinks it sold the full
   entry quantity but Kraken only filled part. The "proceeds" reflect
   a small fill while the "cost" reflects the full entry. The remaining
   quantity is still held but not accounted for in the outcome.
2. **Quantity mismatch at exit**: the exit used a different quantity
   than the entry (e.g., computed from `usd_value / price` with stale
   price data).
3. **Root-exit lumping**: the exit was a forced root_exit that
   reconciled multiple sub-positions into one close record. The bot
   may have mixed USDT bought at different prices.
4. **Stablecoin mis-pricing**: the bot's USD price fallback chain used
   a non-1.0 price for USDT at entry or exit, creating an artificial
   loss.
5. **Currency conversion bug**: if the entry was in USD and the exit
   was in a different quote (unlikely given it's labeled USDT/USD),
   the amounts wouldn't reconcile.

## Desired outcome

1. The specific cause of the −$15.85 trade is identified and documented.
2. If it's a code bug, it is fixed so future USDT/USD trades (or any
   similar stablecoin trades) don't exhibit the same behavior.
3. If it's a data error in `/api/trade-outcomes`, the endpoint is
   corrected or the trade is flagged as anomalous so it doesn't skew
   the self-tune logic (which currently sees this as a "−43% loser").

## Acceptance criteria

1. The brain report or memory entries for the USDT/USD trade are
   located and read. Key timestamps captured: entry time, exit time,
   quantity at entry, quantity at exit, price at entry, price at exit.
2. The root cause is identified in a brief written analysis at
   `tasks/specs/09-usdt-loss-investigation.result.md` covering:
   - Entry details (cycle, txid, qty, price, cost)
   - Exit details (cycle, txid, qty, price, proceeds, reason)
   - The math: why did proc − cost = −$15.85?
   - Which of the hypotheses applies, or a new hypothesis not listed
3. If a code bug is found, it is fixed in the same commit. If the bug
   is in `/api/trade-outcomes` aggregation, a fix is proposed (may be
   in a separate file/module).
4. A regression test or sanity check is added to detect future
   occurrences (e.g., `if pnl_abs_pct > 10% on stablecoin pair → flag`).

## Non-goals

- Do not liquidate any current USDT holdings as "risk mitigation" —
  that's a business decision, not a bug fix.
- Do not change the self-tune thresholds. If the outlier detection
  in acceptance criterion 4 needs a new tuning rule, that's a
  follow-up spec.
- Do not modify the shadow-mode logic or veto.

## Evidence

- `/api/trade-outcomes?lookback_days=7` — shows the outlier row
- `state/cc-reviews/brain_*.md` reports from the entry and exit
  cycles (need to find the specific timestamps)
- `data/bot.db` SQLite — `trades`, `orders`, `positions` tables
  may have more granular records
- `persistence/cc_memory.py` decision memories for USDT/USD entries
