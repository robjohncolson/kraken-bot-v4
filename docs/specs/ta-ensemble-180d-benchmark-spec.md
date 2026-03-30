# TA Ensemble 180d Benchmark Spec

## Purpose

Run the TA ensemble candidate (v1.1) on the 4,320-row CryptoCompare-backed 180d DOGE/USD hourly dataset to establish a benchmark comparison against V1 LogReg (+5,531 bps, Sharpe 11.3).

No TA experiment exists yet on this dataset. The only prior TA runs were on the shorter 30d datasets (v1.0: 0 trades due to history bug, v1.1: 85-400 trades on corrected data).

## Execution

Run from the autoresearch repo against the kraken-bot-v4 dataset:

```bash
cd C:/Users/rober/Downloads/Projects/autoresearch
python -m trading_eval.cli run \
  --candidate ta_ensemble \
  --data-dir C:/Users/rober/Downloads/Projects/kraken-bot-v4/data/research-cc-180d \
  --train-days 90 --val-days 1 --step-days 5 \
  --fee-bps 10 --slippage-bps 5 \
  --horizon 6h \
  --output-dir experiments
```

## Expected output

JSON experiment record in `experiments/ta_ensemble_6signal_<uuid>.json` with:
- 18 walk-forward folds (same config as V1 LogReg: 90d train / 1d val / 5d step)
- Per-fold and aggregate metrics: direction_accuracy, net_pnl_bps, sharpe_ratio, hit_rate, trade_count, abstain_count, brier_score, max_drawdown_bps

## Comparison target

| Metric | V1 LogReg (180d CC) | TA Ensemble (180d CC) |
|--------|--------------------|-----------------------|
| Trades | 214 | TBD |
| Accuracy | 47.2% | TBD |
| Net P&L | +5,531 bps | TBD |
| Sharpe | 11.3 | TBD |
| Hit rate | 53.7% | TBD |

## Decision criteria

- If TA outperforms V1 LogReg: investigate whether the TA ensemble should replace or complement the research model
- If TA underperforms: confirms V1 LogReg as the best available model for DOGE/USD hourly
- Either way, the TA result is a useful second benchmark for future model comparisons

## Sizing

| Step | Effort |
|------|--------|
| Run experiment | ~5 min compute |
| Record results in CONTINUATION_PROMPT.md | 10 min |
| Compare and document decision | 10 min |
