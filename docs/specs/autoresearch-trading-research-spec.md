# Autoresearch Trading Research Spec

## Purpose

Define how the standalone `autoresearch` project should be integrated with `kraken-bot-v4`.

This spec does **not** treat the current `beliefs/technical_ensemble_source.py` module as the real autoresearch system.
That module is a fixed six-signal technical-analysis adapter.

This spec treats the real `..\autoresearch` repo as an **offline autonomous experiment loop** that searches for better trading models, prompts, features, and thresholds, then exports frozen artifacts for the live bot to consume.

## Problem Statement

The current bot has two separate ideas under one name:

- `kraken-bot-v4/beliefs/technical_ensemble_source.py`: fixed TA ensemble
- `..\autoresearch`: autonomous experiment runner that edits code, runs bounded experiments, scores outcomes, and keeps/discards changes

The name collision is misleading.

The right integration is:

- keep the live bot deterministic and artifact-driven
- use actual autoresearch as the offline research and optimization loop
- evaluate candidates on **out-of-sample trading results**, not language-model loss

## Goals

- Turn actual autoresearch into an offline trading-research system.
- Allow a Qwen-class model to produce structured market beliefs.
- Score candidates by realized market outcomes and trading performance.
- Export frozen artifacts that the live bot can load safely.
- Preserve the existing reducer/scheduler/runtime separation in `kraken-bot-v4`.

## Non-Goals

- No self-editing live trading bot.
- No autonomous production deployment.
- No free-form text-only "guidance" as the trading interface.
- No online reinforcement loop tied directly to live capital.
- No replacement of the current reducer contract.

## Core Principle

The model should be optimized as a **forecasting component** and a **decision input**, not as a chatbot.

The live trading bot should consume structured outputs such as:

```json
{
  "pair": "DOGE/USD",
  "direction": "bullish|bearish|neutral",
  "confidence": 0.0,
  "regime": "trending|ranging|unknown",
  "horizon_hours": 6,
  "expected_return_bps": 0.0
}
```

## High-Level Architecture

```text
historical market data + local DB trade outcomes
    -> research dataset builder
    -> autoresearch experiment loop
    -> candidate model/prompt/config
    -> walk-forward evaluation + backtest
    -> keep/discard
    -> frozen artifact
    -> kraken-bot-v4 belief source adapter
    -> scheduler
    -> reducer
```

## Recommended Integration Model

### 1. Keep Research and Execution Separate

The actual `..\autoresearch` repo becomes the research engine.

`kraken-bot-v4` remains the execution engine.

The only boundary between them is a versioned artifact, for example:

- model weights
- LoRA adapter
- prompt template bundle
- feature schema
- inference config
- calibration table
- threshold config

### 2. Add a New Belief Source

Create a new live belief source in `kraken-bot-v4`, for example:

- `beliefs/research_model_source.py`

This module should:

- load the promoted artifact
- build a structured inference input
- call the model locally
- parse structured JSON output
- emit `BeliefSnapshot`

The current fixed TA module is:

- `beliefs/technical_ensemble_source.py`

## What Actual Autoresearch Should Optimize

Actual autoresearch should not optimize for eloquence.

It should optimize for objective, scored outcomes.

### Primary Targets

- next-horizon direction accuracy
- expected-return quality
- confidence calibration
- regime classification accuracy
- strategy P&L after fees and slippage
- drawdown control
- stability across walk-forward windows

### Candidate Output Schema

Each candidate should emit:

- `direction`
- `confidence`
- `regime`
- `horizon_hours`
- `expected_return_bps`

Optional additions:

- `uncertainty`
- `stop_suggestion_bps`
- `target_suggestion_bps`
- `abstain`

## Data Sources

### Required

- Kraken OHLCV history
- local DB fills
- local DB orders
- local DB closed-trade outcomes
- fee data
- slippage estimates from realized execution

### Optional

- order-book summaries
- funding or borrow data if margin is ever introduced
- news or social context
- cross-asset regime context

## Training and Evaluation Dataset

### Feature Time Origin

Every sample must be built using only information available at time `t`.

No feature may use:

- future candles
- future fills
- future trade outcomes
- post-hoc labels

### Labels

Recommended first labels:

- sign of return over next `6h`
- sign of return over next `12h`
- realized return in basis points over next `6h`
- realized return in basis points over next `12h`
- regime label derived from realized volatility/trend over next window

### Why Not "Yesterday Only"

A single prior day is too noisy.

Use rolling windows instead:

- train/tune on a historical window, e.g. 30-180 days
- validate on the next 1-5 days
- roll forward repeatedly

This is the minimum acceptable walk-forward structure.

## Experiment Loop

Actual autoresearch should keep its original pattern:

1. choose a candidate change
2. run a bounded experiment
3. score the candidate
4. keep or discard

But the score changes from `val_bpb` to trading-specific metrics.

### Candidate Search Space

Candidates may vary:

- prompt format
- feature packing
- label horizon
- temperature or decoding constraints
- LoRA hyperparameters
- fine-tuning settings
- calibration method
- threshold rules for acting vs abstaining

### Candidate Scorecard

Each run should log:

- out-of-sample net P&L
- max drawdown
- Sharpe or Sortino
- turnover
- hit rate
- confidence calibration error
- number of abstentions
- stability across windows

## Evaluation Protocol

### Stage 1: Prediction Quality

Measure:

- cross-entropy for direction labels
- Brier score for calibrated probabilities
- mean absolute error for expected return
- regime classification accuracy

### Stage 2: Decision Quality

Map predictions to trading actions and evaluate:

- net P&L after fees
- slippage-adjusted P&L
- max drawdown
- win/loss distribution
- P&L stability across windows

### Stage 3: Promotion Gate

A candidate is promoted only if it beats the incumbent on:

- multiple walk-forward windows
- net return after costs
- drawdown-adjusted metrics
- calibration

No candidate is promoted based on one lucky slice.

## Model Choice Guidance

### Qwen-Class LLMs

A Qwen-class model can make sense if it is used for:

- structured belief generation
- reasoning over mixed data types
- fusing text with market data
- regime synthesis across multiple inputs

It is a worse fit if it is used as:

- the first baseline on OHLCV-only data
- a free-form trading chatbot
- an online reinforcement learner on sparse live outcomes

### Recommendation

Start with simpler baselines first:

- technical ensemble
- linear/logistic models
- gradient-boosted trees
- small forecasting nets

Only keep the Qwen path if it wins out of sample.

## Artifact Contract

Each promoted research artifact should contain:

- artifact version
- model family
- label horizon
- feature schema version
- expected input schema
- output schema
- calibration metadata
- training window summary
- evaluation summary
- commit hash from autoresearch repo
- promotion timestamp

Example:

```json
{
  "artifact_version": "2026-03-25-qwen-doge-v1",
  "model_family": "qwen-4b-class",
  "horizon_hours": 6,
  "feature_schema": "market-v1",
  "output_schema": "belief-v1",
  "calibration": {
    "method": "isotonic"
  },
  "evaluation": {
    "net_pnl": 0.0,
    "max_drawdown": 0.0,
    "sharpe": 0.0
  }
}
```

## Live Bot Integration

### New Belief Source

`kraken-bot-v4` should load the promoted artifact through a dedicated source module.

Responsibilities:

- prepare inference input from current market state
- call the model locally
- parse structured output
- reject malformed output
- map output to `BeliefSnapshot`

### Consensus

The research-model belief should participate in the same consensus mechanism as other sources.

Recommended initial rollout:

- keep the current fixed TA source
- add research-model source as an independent vote
- require consensus before acting

This prevents a new model from becoming a single point of failure on day one.

## Safety Requirements

- No automatic promotion into production.
- No live code edits from autoresearch into `kraken-bot-v4`.
- No use of future data in features.
- All backtests must include fees and slippage.
- All candidate metrics must be logged.
- All promoted artifacts must be reproducible from a commit and dataset snapshot.

## Suggested First Iteration

### Scope

Do not begin with full fine-tuning of a large model.

Phase 1 should be:

- export a structured dataset from historical OHLCV plus local DB outcome data
- define labels for `6h` and `12h` direction
- create a baseline scorer using the current fixed TA source
- adapt actual autoresearch to optimize a small set of candidate prompt/config choices
- evaluate a Qwen-class model only after the evaluation harness is stable

### First Output Contract

The first production-safe contract should be:

- `direction`
- `confidence`
- `regime`
- `horizon_hours`

`expected_return_bps` can be added once calibration is trustworthy.

## Open Questions

- Should the first research target be prompt optimization, LoRA fine-tuning, or both?
- Which horizon should be primary: `6h`, `12h`, or `24h`?
- Should DOGE-only be the initial focus, or should the dataset be multi-pair from day one?
- Should the first model use market-only data, or include text/news context immediately?
- What minimum out-of-sample sample size is required before promotion?

## Recommendation

Use actual autoresearch as the **offline strategy/model research loop**.

Do not try to turn the live bot itself into an autonomous self-editing trader.

The right fit is:

- autoresearch searches
- backtests judge
- humans approve
- `kraken-bot-v4` executes frozen artifacts
