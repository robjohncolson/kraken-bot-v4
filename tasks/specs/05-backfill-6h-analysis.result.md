# Backfill 6h analysis - result

## Run metadata
- Command: `python scripts/backfill_shadow.py --forward-hours 6`
- Date: `2026-04-12T13:36:15Z`
- Reports processed: 54 `brain_*.md` reports
- Cycles analyzed: 33 parseable cycles
- Reconstruction fidelity: 665/665 (100%) within 0.05 of logged score
- Cycles with 6h forward data: 19

Note: the command ran cleanly through reconstruction and summary, but its direct Kraken fetch was blocked by the shell sandbox (`WinError 10013`). The 6h forward-return table below was reconstructed from the live bot's local `http://127.0.0.1:58392/api/ohlcv/...` endpoint, which exposes the same Kraken hourly OHLC series that `exchange/ohlcv.py` uses. Candle alignment was the hour containing each cycle timestamp and the close 6 hours later, matching the backfill script's post-hoc hourly comparison behavior.

## Backfill summary
- Live decision types: 19 `entry`, 14 `exit`
- Shadow best-hold distribution across 33 analyzed cycles: `USD` 21, `BTC` 7, `ETH` 4, `ADA` 1
- Agreement on order cycles: 0/19 (0%)
- Eligibility coverage: `USD` 33/33, `ETH` 12/33, `BTC` 11/33, `ALGO` 8/33, `USDT` 8/33, `ADA` 4/33, `BCH` 2/33, `USDC` 1/33

## Raw per-cycle table
| Cycle UTC | Live | Live 6h ret | Shadow | Shadow 6h ret | Edge (shadow - live) | Verdict |
|-----------|------|-------------|--------|---------------|----------------------|---------|
| 2026-04-11 22:49 | MON/USD | -4.56% | USD | +0.00% | +4.56% | SHADOW |
| 2026-04-11 22:53 | MON/USD | -4.56% | USD | +0.00% | +4.56% | SHADOW |
| 2026-04-11 23:20 | ZEC/USD | -2.61% | USD | +0.00% | +2.61% | SHADOW |
| 2026-04-11 23:23 | ZEC/USD | -2.61% | USD | +0.00% | +2.61% | SHADOW |
| 2026-04-11 23:25 | ZEC/USD | -2.61% | USD | +0.00% | +2.61% | SHADOW |
| 2026-04-11 23:27 | MON/USD | -2.36% | USD | +0.00% | +2.36% | SHADOW |
| 2026-04-12 00:30 | MON/USD | -3.27% | USD | +0.00% | +3.27% | SHADOW |
| 2026-04-12 01:32 | RAVE/USD | +35.25% | USD | +0.00% | -35.25% | LIVE |
| 2026-04-12 02:12 | SOL/USD | -0.50% | USD | +0.00% | +0.50% | SHADOW |
| 2026-04-12 02:16 | XRP/USD | -0.42% | USD | +0.00% | +0.42% | SHADOW |
| 2026-04-12 02:21 | APE/USDT | -0.46% | BTC/USD | -0.21% | +0.25% | SHADOW |
| 2026-04-12 02:35 | XRP/USD | -0.42% | USD | +0.00% | +0.42% | SHADOW |
| 2026-04-12 02:36 | APE/USDT | -0.46% | ETH/USD | -0.44% | +0.02% | tie |
| 2026-04-12 02:43 | APE/USDT | -0.46% | BTC/USD | -0.21% | +0.25% | SHADOW |
| 2026-04-12 03:28 | RENDER/USD | -1.76% | BTC/USD | -0.08% | +1.69% | SHADOW |
| 2026-04-12 03:37 | SUI/USD | -0.12% | USD | +0.00% | +0.12% | SHADOW |
| 2026-04-12 03:49 | RENDER/USD | -1.76% | BTC/USD | -0.08% | +1.69% | SHADOW |
| 2026-04-12 03:56 | SUI/USD | -0.12% | BTC/USD | -0.08% | +0.04% | tie |
| 2026-04-12 04:03 | HYPE/USD | -0.86% | BTC/USD | -0.02% | +0.83% | SHADOW |

## Aggregate metrics
| Metric | Value |
|--------|-------|
| Shadow wins | 16 / 19 (84.2%) |
| Live wins | 1 / 19 (5.3%) |
| Ties | 2 / 19 (10.5%) |
| Non-tie disagreement correctness | 16 / 17 shadow (94.1%) |
| Cumulative live return | +5.30% |
| Cumulative shadow return | -1.12% |
| Cumulative edge (shadow - live) | -6.42% |
| Avg per-cycle edge (shadow - live) | -0.34% |
| Binomial 1-sided p | 0.00014 on non-tie disagreements |

Interpretation: the longer-window win-rate signal still strongly favors shadow, but the net-return signal flipped. One live outlier, `RAVE/USD` at `2026-04-12 01:32 UTC`, contributed `+35.25%` over 6h and overwhelmed many smaller shadow avoids.

## Comparison to 2h window result
- 2h window: 6/6 shadow, cumulative edge `+17.80%` (shadow over live), heavily clustered within one ~40 minute slice
- 6h window: 16/19 shadow, 1/19 live, 2 ties, but cumulative edge `-6.42%` (shadow minus live)
- Directional signal: yes, shadow still wins most disagreements
- Net-return signal: no, the 2h result did not hold on cumulative edge once the window widened to 6h

## Sample-size assessment
- This is materially better than the first pass on raw count: 19 evaluable cycles instead of 6
- It also spans 9 distinct live pairs: `MON`, `ZEC`, `RAVE`, `SOL`, `XRP`, `APE`, `RENDER`, `SUI`, `HYPE`
- It is still not 19 independent ideas. The 19 cycles collapse to 14 strict consecutive same-pair clusters, or about 11 looser same-pair retry clusters if retries within 30 minutes are merged
- Coverage for promotion is still weak. Across all 33 analyzed cycles, shadow produced only 4 distinct best holds (`USD`, `BTC`, `ETH`, `ADA`), below the `>= 5` bar in [tasks/shadow_promotion_criterion.md](/C:/Users/rober/Downloads/Projects/kraken-bot-v4/state/parallel-worktrees/05-backfill-analysis/tasks/shadow_promotion_criterion.md)
- Agreement quality also fails the stated promotion gate: order-cycle agreement is 0/19, well below the `< 50%` "investigate root cause first" branch in the same criterion

## Disagreement pattern
- `USD -> MON/USD` happened 4 times and shadow was right every time; MON lost between `-2.36%` and `-4.56%` while cash was flat
- `USD -> ZEC/USD` happened 3 times and shadow was right every time; ZEC lost `-2.61%` in each 6h window
- `BTC/ETH -> APE/USDT` happened 3 times; shadow won twice and tied once, but only by `0.02%` to `0.25%`
- `BTC -> RENDER/USD` happened 2 times and shadow was right both times by `1.69%`
- `USD -> RAVE/USD` happened once and live was decisively right by `35.25%`; this single miss flips the aggregate P&L back in live's favor

## Recommendation
**Investigate**

Reasoning:
- The shadow path is not ready for promotion: agreement is 0%, coverage criteria are not met, and cumulative 6h edge is negative
- The shadow signal is still too strong to dismiss as noise: it won 16 of 17 non-tie disagreements
- Those two facts together point to a root-cause problem, not a clean promote/hold decision. The obvious follow-up is to explain why the unified hold scorer preferred `USD` over the one large `RAVE/USD` winner and whether that is a real bias or a one-off regime miss

## Follow-up specs
- None created in this task. If this result is acted on, the next spec should focus on diagnosing the `RAVE/USD` miss and the broader "shadow prefers cash too often" bias before expanding veto scope.
