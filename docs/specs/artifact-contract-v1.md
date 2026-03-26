# Artifact Contract v1

## Purpose

Defines the interface between the `autoresearch` evaluation harness (producer) and `kraken-bot-v4` (consumer). A promoted artifact is a frozen, versioned bundle that the bot can load to generate trading beliefs without consulting training code.

## Artifact Directory Structure

```
artifacts/<artifact_id>/
  manifest.json       # Artifact metadata and evaluation summary
  experiment.json     # Full experiment record for auditability
  model/              # Model weights, adapter, or config (if applicable)
```

## Manifest Schema (manifest.json)

```json
{
  "artifact_id": "ta_ensemble_6signal_20260325_abc12345",
  "artifact_version": "1.0",
  "model_family": "ta_ensemble_6signal",
  "input_schema_version": "market/v1",
  "output_schema_version": "prediction/v1",
  "label_horizon": "6h",
  "calibration": {
    "method": "none"
  },
  "evaluation_summary": {
    "direction_accuracy": 0.52,
    "brier_score": 0.25,
    "mae_bps": 45.0,
    "net_pnl_bps": 120.0,
    "max_drawdown_bps": 80.0,
    "sharpe_ratio": 0.85,
    "sortino_ratio": 1.2,
    "turnover": 0.65,
    "hit_rate": 0.54,
    "trade_count": 500,
    "abstain_count": 200
  },
  "source_commit": "abc123def456",
  "experiment_id": "abc12345",
  "created_at": "2026-03-25T14:30:45+00:00"
}
```

## Field Descriptions

| Field | Type | Description |
|-------|------|-------------|
| `artifact_id` | string | Unique identifier: `{model_family}_{date}_{experiment_id}` |
| `artifact_version` | string | Semantic version of this artifact |
| `model_family` | string | Candidate name (e.g., `ta_ensemble_6signal`, `logistic_regression`) |
| `input_schema_version` | string | Expected input format: `"market/v1"` = OHLCV with timestamp |
| `output_schema_version` | string | Output format: `"prediction/v1"` = signal + confidence + prob_up |
| `label_horizon` | string | Which label horizon was used for evaluation (e.g., `"6h"`, `"12h"`) |
| `calibration` | object | Calibration method applied to model outputs |
| `evaluation_summary` | object | Aggregate walk-forward metrics (see Metrics below) |
| `source_commit` | string | Git commit hash from the autoresearch repo |
| `experiment_id` | string | Links to the full experiment record |
| `created_at` | string | ISO 8601 timestamp of promotion |

## Input Schema: market/v1

The consumer (future `beliefs/research_model_source.py`) must provide:

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | int64 | Unix epoch seconds |
| `open` | float64 | Candle open price |
| `high` | float64 | Candle high price |
| `low` | float64 | Candle low price |
| `close` | float64 | Candle close price |
| `volume` | float64 | Candle volume |

Candles are hourly (interval=60). Minimum 40 bars required for TA-based models.

## Output Schema: prediction/v1

Each artifact's inference function must return:

| Field | Type | Values |
|-------|------|--------|
| `signal` | int | -1 (SHORT), 0 (ABSTAIN), 1 (LONG) |
| `confidence` | float | 0.0 to 1.0 |
| `prob_up` | float | 0.0 to 1.0, calibrated P(price goes up) |

The consumer maps this to `BeliefSnapshot`:
- signal=1 -> BULLISH, signal=-1 -> BEARISH, signal=0 -> NEUTRAL
- confidence maps directly
- regime derived from model-specific logic or defaulted to UNKNOWN

## Metrics in evaluation_summary

| Metric | Direction | Description |
|--------|-----------|-------------|
| `direction_accuracy` | higher=better | Fraction correct direction predictions |
| `brier_score` | lower=better | Calibration quality of prob_up |
| `mae_bps` | lower=better | Mean absolute error of actual returns |
| `net_pnl_bps` | higher=better | Cumulative P&L after fees+slippage |
| `max_drawdown_bps` | lower=better | Largest peak-to-trough in cumulative P&L |
| `sharpe_ratio` | higher=better | Risk-adjusted return (annualized) |
| `sortino_ratio` | higher=better | Downside-risk-adjusted return |
| `turnover` | informational | Fraction of periods with non-abstain signal |
| `hit_rate` | higher=better | Fraction of trades with positive P&L |

## How kraken-bot-v4 Will Load Artifacts (Phase 6)

Future `beliefs/research_model_source.py` will:

1. Read `ACTIVE_ARTIFACT_ID` from config/env
2. Load `artifacts/<id>/manifest.json`
3. Validate `input_schema_version == "market/v1"` and `output_schema_version == "prediction/v1"`
4. Load model from `artifacts/<id>/model/` (if applicable)
5. On each belief refresh: build market input from current OHLCV, run inference, validate output, map to `BeliefSnapshot`
6. Reject malformed outputs with logging (never crash the runtime)

## Safety Rules

- Artifacts are frozen — no in-place modification after promotion
- Promotion is explicit and manual (no auto-deployment)
- The active artifact ID is set in bot config, not determined by the artifact itself
- Model outputs are validated before reaching the reducer
- Malformed outputs are logged and rejected, never forwarded
