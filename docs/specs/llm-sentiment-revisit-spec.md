# LLM Revisit: News/Sentiment Inputs

## Background

Phase 5a evaluated a prompted LLM (Qwen3 8B) on raw OHLCV features. Result: -167 bps, 50% hit rate, 8 trades. The LLM could not extract signal from tabular numeric data that LogReg already captures.

**Hypothesis**: LLMs add value on unstructured text (news, social sentiment, on-chain narratives), not on structured numeric features where statistical models already work well. A revisit should use fundamentally different inputs.

## Proposed approach

### Input sources (ranked by feasibility)

1. **CryptoCompare News API** (`/data/v2/news/`)
   - Free tier: 100k calls/month
   - Returns: title, body, categories, source, published_at
   - Filter by coin (DOGE) or category
   - Available historically for backtesting

2. **Reddit/Twitter sentiment aggregators**
   - LunarCrush, Santiment, or The TIE APIs
   - Pre-computed sentiment scores (bullish/bearish/neutral)
   - Some have free tiers; others require paid plans
   - Historical data varies by provider

3. **On-chain metrics** (Glassnode, IntoTheBlock)
   - Active addresses, exchange inflows/outflows, whale transactions
   - Mostly paid; free tiers have limited history
   - More relevant for BTC/ETH than DOGE

### Prompt design

The LLM receives a structured prompt with:
- Last 24h of news headlines (5-10 most relevant)
- Optional: sentiment scores from aggregator
- Current OHLCV summary (1-line context, not full feature set)
- Task: predict 6h direction (bullish/bearish/neutral) with confidence

```
You are a crypto market analyst. Based on recent news and market context,
predict DOGE/USD direction over the next 6 hours.

Recent news (last 24h):
1. "Elon Musk tweets about Dogecoin..." (2h ago, source: CoinDesk)
2. "DOGE whale moves 500M coins to exchange" (5h ago, source: Whale Alert)
3. ...

Current market: DOGE/USD at $0.18, up 2.3% in 24h, RSI 55

Respond with JSON: {"direction": "bullish"|"bearish"|"neutral", "confidence": 0.0-1.0, "reasoning": "..."}
```

### Evaluation methodology

1. **Data collection**: Fetch historical news for the 180d window (CryptoCompare News API)
2. **Candidate implementation**: New `LLMSentimentCandidate` in autoresearch that:
   - Receives OHLCV market data (for timestamp alignment)
   - Fetches corresponding news headlines for that timestamp window
   - Constructs prompt, calls local Ollama model
   - Parses structured output into Prediction
3. **Walk-forward evaluation**: Same harness as V1 LogReg (90d train / 1d val / 5d step)
   - Train phase: no training needed (prompted, not fine-tuned)
   - Val phase: generate predictions, backtest against actual returns
4. **Comparison**: Against V1 LogReg (+5,531 bps) and TA ensemble

### Integration with existing infrastructure

- **Artifact contract**: Same `prediction/v1` output schema
- **Shadow mode**: Same handler pattern as research_model_handler.py
- **Model**: Qwen3 8B via local Ollama (IPEX-LLM on Intel Arc GPU)

## Risks

- **News availability**: CryptoCompare may not have sufficient DOGE-specific news density
- **Latency**: LLM inference (~2-5s per prediction) is slower than LogReg (~1ms)
- **Reproducibility**: LLM outputs are stochastic; need temperature=0 or seed
- **Cost**: Local inference is free but slow; cloud inference adds cost

## Phasing

| Phase | Deliverable | Effort |
|-------|-------------|--------|
| P1: Data audit | Check CryptoCompare news density for DOGE over 180d | 1 session |
| P2: News fetcher | `research/news_cryptocompare.py` + paginated historical fetch | 1 session |
| P3: Candidate | `LLMSentimentCandidate` in autoresearch | 1-2 sessions |
| P4: Evaluation | Walk-forward on 180d dataset, compare to V1 LogReg | 1 session |
| P5: Integration | Shadow handler if results are promising | 1 session |

## Decision gate

After P4, compare against V1 LogReg:
- If LLM-sentiment outperforms: proceed to P5 (shadow integration)
- If LLM-sentiment underperforms but shows complementary signal: investigate ensemble
- If LLM-sentiment adds nothing: close the LLM research track

## Non-goals

- Fine-tuning the LLM (too much compute for marginal gains on this scale)
- Real-time news streaming (batch evaluation first)
- Multi-pair sentiment scanning (DOGE only for now)
