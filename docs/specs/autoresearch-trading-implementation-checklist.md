# Autoresearch Trading Implementation Checklist

## Objective

Turn the real `..\autoresearch` project into an offline trading-research loop that produces frozen artifacts for `kraken-bot-v4` to consume as a belief source.

This checklist is ordered so the system becomes useful early, while keeping live trading risk bounded.

## Phase 0: Clarify Naming

### Tasks

- Keep the fixed TA source under `beliefs/technical_ensemble_source.py`.
- Keep the corresponding handler under a matching `technical_ensemble_*` name.
- Keep imports, tests, and docs aligned so "AutoResearch" refers only to the real external research repo.

### Deliverables

- Clear separation between:
  - fixed TA belief source
  - offline autonomous research loop

### Acceptance Criteria

- No code path in `kraken-bot-v4` uses the word "autoresearch" for the fixed six-signal TA source.
- Existing belief-source tests still pass.

## Phase 1: Define the Research Dataset

### Tasks

- Add a dataset builder that exports time-indexed samples from:
  - Kraken OHLCV history
  - local DB orders
  - local DB fills
  - local DB closed trade outcomes
- Freeze features at time `t` only.
- Add labels for at least:
  - `return_sign_6h`
  - `return_sign_12h`
  - `return_bps_6h`
  - `return_bps_12h`
  - `regime_label`
- Version the dataset schema.

### Suggested Output

- `data/research/market_v1.parquet`
- `data/research/labels_v1.parquet`
- `data/research/manifest_v1.json`

### Acceptance Criteria

- A sample can be reproduced from raw source data.
- No feature uses future candles or post-hoc trade outcomes.
- Dataset generation is deterministic for a fixed input snapshot.

## Phase 2: Build the Evaluation Harness

### Tasks

- Create a walk-forward evaluator in `..\autoresearch`.
- Define rolling windows:
  - train/tune on 30-180 days
  - validate on next 1-5 days
  - roll forward repeatedly
- Add backtest scoring with:
  - fees
  - slippage
  - abstain support
- Log both prediction and decision metrics.

### Required Metrics

- direction accuracy
- Brier score or calibration error
- MAE on expected return
- net P&L after costs
- max drawdown
- Sharpe or Sortino
- turnover
- hit rate

### Acceptance Criteria

- One command runs a complete walk-forward experiment.
- Metrics are stored per window and in aggregate.
- Results can compare a candidate against a baseline.

## Phase 3: Establish Baselines Before Qwen

### Tasks

- Port the current six-signal TA ensemble into the evaluation harness as baseline A.
- Add at least one simple non-LLM baseline:
  - logistic regression
  - gradient-boosted tree
  - simple rules
- Score these first before introducing a Qwen-based candidate.

### Acceptance Criteria

- The harness can rank at least two non-LLM baselines.
- Baseline results are saved as reproducible experiment records.

## Phase 4: Define the Artifact Contract

### Tasks

- Define a versioned artifact schema for promoted candidates.
- Include:
  - artifact version
  - model family
  - input schema version
  - output schema version
  - label horizon
  - calibration metadata
  - evaluation summary
  - source commit hash
- Store artifacts in a stable location the bot can load from.

### Suggested Files

- `artifacts/<artifact_id>/manifest.json`
- `artifacts/<artifact_id>/config.json`
- `artifacts/<artifact_id>/weights/` or `adapter/`

### Acceptance Criteria

- A promoted artifact can be loaded without consulting training code.
- Artifact metadata is enough to audit how it was produced.

## Phase 5: Add the Qwen Research Path

### Tasks

- Add a structured inference format for a Qwen-class model.
- Constrain outputs to JSON with:
  - `direction`
  - `confidence`
  - `regime`
  - `horizon_hours`
  - optional `expected_return_bps`
- Evaluate prompt-only and fine-tuned variants separately.
- Calibrate confidence after raw model scoring.

### Guardrails

- Do not score persuasive text quality.
- Do not allow free-form outputs into the trading bot.
- Do not promote a Qwen artifact unless it beats simpler baselines out of sample.

### Acceptance Criteria

- Model outputs are parseable and validated.
- Confidence is calibration-tested, not just accepted at face value.
- Qwen candidates have reproducible evaluation records.

## Phase 6: Add a New Live Belief Source

### Tasks

- Add a dedicated source module in `kraken-bot-v4`, for example:
  - `beliefs/research_model_source.py`
- Load a promoted artifact from disk.
- Build a structured inference input from current market state.
- Parse and validate model output.
- Convert valid output into `BeliefSnapshot`.

### Scope Rules

- No self-editing.
- No training in the bot process.
- No direct runtime mutation of strategy code.

### Acceptance Criteria

- The bot can load an artifact and emit a valid `BeliefSnapshot`.
- Malformed model output is rejected safely with logs.

## Phase 7: Roll Out Through Consensus

### Tasks

- Keep the fixed TA source enabled.
- Add the research-model source as an additional vote.
- Require the existing consensus path before acting.
- Log per-source outputs so disagreements are visible.

### Acceptance Criteria

- The new source can run live without becoming a single point of failure.
- Dashboard and logs show which sources voted and why the reducer acted or abstained.

## Phase 8: Promotion Workflow

### Tasks

- Add a manual promotion process for moving a research artifact into live use.
- Require:
  - walk-forward results
  - drawdown review
  - calibration review
  - commit hash and dataset snapshot
- Store the active artifact id in bot config.

### Acceptance Criteria

- Promotion is explicit and reversible.
- The active artifact can be traced back to its experiment record.

## Phase 9: Post-Deployment Monitoring

### Tasks

- Log live predictions and realized outcomes for later calibration review.
- Track:
  - prediction distribution
  - abstain rate
  - realized hit rate by confidence bucket
  - live P&L contribution by source
- Detect drift between backtest behavior and live outcomes.

### Acceptance Criteria

- Live forecasts can be compared against realized returns by horizon.
- Confidence calibration can be re-measured from production data.

## Recommended File Ownership

### `kraken-bot-v4`

- `beliefs/research_model_source.py`
- loader/config wiring
- live inference adapter
- consensus integration
- monitoring/logging

### `..\autoresearch`

- dataset builder
- experiment runner
- walk-forward harness
- candidate scoring
- artifact export

## First Safe Milestone

The first milestone worth shipping is:

- renamed TA source
- dataset export
- walk-forward harness
- current TA baseline inside the harness
- one simple non-LLM baseline
- artifact schema

Do not start with live Qwen trading. Start by proving the evaluation loop works.

## Second Milestone

- Qwen structured forecast candidate
- calibrated confidence
- offline comparison against baselines
- no live deployment unless it wins out of sample

## Third Milestone

- live `research_model_source`
- consensus-gated rollout
- paper-trade or read-only observation mode
- live promotion only after monitored validation

## Explicit Stop Conditions

Pause the rollout if any of these are true:

- backtests exclude fees or slippage
- confidence is uncalibrated
- candidate wins only on one narrow slice
- the model cannot emit stable structured output
- the live bot would need to self-modify to use the artifact

## Immediate Next Actions

1. Rename the current TA source so terminology is no longer misleading.
2. Build the dataset export from OHLCV plus local DB outcomes.
3. Build the walk-forward evaluator in `..\autoresearch`.
4. Port the current TA source into that harness as the first baseline.
5. Add one simple non-LLM baseline before any Qwen experiment.
