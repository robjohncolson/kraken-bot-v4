# LLM Market Context Advisor (Revised)

## Background

Phase 5a evaluated a prompted LLM (Qwen3 8B) on raw OHLCV features. Result: -167 bps, 50% hit rate, 8 trades. The original spec proposed using CryptoCompare news/sentiment, but:

1. CryptoCompare News API requires a paid key
2. Grid-bot-v3 (`not-school/doge-grid-bot`) proved a more effective approach: feed LLMs **structured market context** (not news), using **free-tier API providers** with majority voting

## V3's Proven Approach

Grid-bot-v3's `ai_advisor.py` (1900 lines) fed LLMs:
- Current price + 1h/4h/24h changes
- HMM regime signals (bearish/ranging/bullish across 1m/15m/1h)
- Technical indicators (EMA, RSI, MACD, Bollinger)
- Grid metrics (fill counts, position age distribution)
- Capital allocation and throughput analysis

Using a **multi-model council** with majority voting:
- DeepSeek (primary reasoning model)
- Groq (Llama 3.3 70B fallback)
- SambaNova, Cerebras, NVIDIA (free-tier panel)

**No news APIs, no paid keys, no external sentiment data.**

## Proposed Architecture for V4

### Input Context (structured JSON)

Build a `MarketContext` from existing V4 data sources:

```python
{
    "pair": "DOGE/USD",
    "price": 0.0918,
    "changes": {"1h": -0.3, "4h": 1.2, "24h": -2.1},
    "technical": {
        "ema_crossover": false,
        "rsi": 45.2,
        "macd_histogram": -0.00012,
        "bollinger_width": 0.034,
        "momentum_12h": false,
        "volatility": 0.018
    },
    "portfolio": {
        "total_value_usd": 502,
        "cash_usd": 40,
        "doge_qty": 5089,
        "open_positions": 0
    },
    "v1_model": {
        "direction": "neutral",
        "prob_up": 0.48,
        "confidence": 0.04
    }
}
```

Data sources: `exchange/ohlcv.py` (OHLCV), `beliefs/technical_ensemble_source.py` (signals), `beliefs/research_model_source.py` (V1 prob_up), portfolio from bot state.

### LLM Council

#### Free-tier providers (no paid API keys):

| Provider | Model | Free Tier | Endpoint |
|----------|-------|-----------|----------|
| Groq | Llama 3.3 70B | Free (rate limited) | api.groq.com |
| SambaNova | DeepSeek-R1 | Free | api.sambanova.ai |
| Cerebras | Qwen3 235B | Free (1M tokens/day) | api.cerebras.ai |
| Local Ollama | Qwen3 8B | Free (local GPU) | localhost:11434 |

#### Voting mechanism:
- Each panelist receives identical structured context
- Returns JSON: `{"direction": "bullish"|"bearish"|"neutral", "conviction": 0.0-1.0, "reasoning": "..."}`
- Majority vote determines consensus direction
- If no majority: defaults to V1 model's signal
- Circuit breaker: skip panelist after 3 consecutive failures

### Integration

New belief source: `beliefs/llm_council_source.py`
- Implements `BeliefAnalyzer` protocol
- Polls every `LLM_COUNCIL_INTERVAL_SEC` (default: 1800 = 30min)
- Returns `BeliefSnapshot` with `source=LLM_COUNCIL`
- Can run as shadow (like research model) or as a third consensus voter

### Evaluation

Use the existing walk-forward harness in autoresearch:
- New `LLMCouncilCandidate` wraps the council logic
- Backtest on 180d CC-backed dataset
- Compare against V1 LogReg (+5,531 bps) and TA ensemble (+257 bps)

### Phasing

| Phase | Deliverable | Effort |
|-------|-------------|--------|
| P1: Provider setup | Sign up for Groq/SambaNova/Cerebras free keys | 30 min |
| P2: Council module | `beliefs/llm_council_source.py` + structured context builder | 1 session |
| P3: Shadow integration | Wire as shadow handler, log predictions | 30 min |
| P4: Backtest candidate | `LLMCouncilCandidate` in autoresearch | 1 session |
| P5: Evaluation | Walk-forward on 180d, compare to V1 LogReg | 1 session |

### Decision Gate

After P5, compare against V1 LogReg:
- If LLM council outperforms: promote as primary or ensemble member
- If LLM council adds complementary signal: investigate weighted ensemble
- If LLM council adds nothing: close the LLM track, V1 LogReg remains winner

### Prerequisites

User needs to sign up for free API keys:
- Groq: https://console.groq.com
- SambaNova: https://cloud.sambanova.ai
- Cerebras: https://cloud.cerebras.ai

No paid subscriptions required. All offer free tiers sufficient for hourly polling.
