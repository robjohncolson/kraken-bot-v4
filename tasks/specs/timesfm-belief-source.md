# TimesFM Belief Source — Integration Spec

## Goal
Add Google's TimesFM 2.5 (200M) as a new belief source that predicts price direction
from close-price history, producing `BeliefSnapshot` objects that feed into the
existing consensus system alongside technical_ensemble and LLM council sources.

## Architecture

### New files
- `beliefs/timesfm_source.py` — the belief source (follows `TechnicalEnsembleSource` pattern)
- `tests/beliefs/test_timesfm_source.py` — unit tests

### Integration point
The source implements the same interface as `TechnicalEnsembleSource`:
- Takes a pair name and a `pd.DataFrame` of OHLCV bars (1h candles)
- Returns a `BeliefSnapshot` with direction, confidence, and regime
- Gets added to the belief orchestrator's source list

## TimesFM Source Design

```python
class TimesFMSource:
    """Belief source using Google TimesFM 2.5 for close-price forecasting."""

    def __init__(self, horizon: int = 24, context_length: int = 512):
        """Load the model once at init. Lazy-load to avoid import cost."""
        self.horizon = horizon          # predict 24h ahead
        self.context_length = context_length  # ~21 days of 1h candles
        self._model = None              # lazy init

    def _ensure_model(self):
        """Lazy-load TimesFM model on first use."""
        if self._model is not None:
            return
        import timesfm
        self._model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
            "google/timesfm-2.5-200m-pytorch",
            torch_compile=False,  # Skip compile overhead
        )
        self._model.compile(timesfm.ForecastConfig(
            max_context=self.context_length,
            max_horizon=self.horizon,
            normalize_inputs=True,
            use_continuous_quantile_head=True,
            force_flip_invariance=False,  # Halves inference time
            infer_is_positive=True,       # Prices are always positive
        ))

    def analyze(self, pair: str, bars: pd.DataFrame) -> BeliefSnapshot:
        """Generate belief from close-price forecast."""
        self._ensure_model()
        close = bars["close"].values.astype(np.float32)

        point_forecast, quantile_forecast = self._model.forecast(
            horizon=self.horizon,
            inputs=[close[-self.context_length:]],
        )

        current_price = float(close[-1])
        # Median forecast at horizon end
        predicted_price = float(point_forecast[0, -1])
        # 10th and 90th percentile at horizon end
        p10 = float(quantile_forecast[0, -1, 1])  # bearish bound
        p90 = float(quantile_forecast[0, -1, 9])  # bullish bound

        # Direction: compare median forecast to current
        pct_change = (predicted_price - current_price) / current_price

        if pct_change > 0.005:      # >0.5% predicted rise
            direction = BeliefDirection.BULLISH
        elif pct_change < -0.005:   # >0.5% predicted drop
            direction = BeliefDirection.BEARISH
        else:
            direction = BeliefDirection.NEUTRAL

        # Confidence: how tight is the quantile spread relative to direction
        # If p10 > current (even bearish bound is above current), high confidence bullish
        # If p90 < current (even bullish bound is below current), high confidence bearish
        spread = (p90 - p10) / current_price
        if direction == BeliefDirection.BULLISH:
            confidence = min(1.0, max(0.3, (p10 - current_price) / current_price * 10 + 0.5))
        elif direction == BeliefDirection.BEARISH:
            confidence = min(1.0, max(0.3, (current_price - p90) / current_price * 10 + 0.5))
        else:
            confidence = 0.0

        regime = MarketRegime.TRENDING if spread > 0.02 else MarketRegime.RANGING

        return BeliefSnapshot(
            pair=pair,
            direction=direction,
            confidence=round(confidence, 2),
            source="timesfm",
            regime=regime,
        )
```

## Dependencies

```bash
# Intel Arc GPU support
pip install intel-extension-for-pytorch
# TimesFM itself
pip install timesfm
```

The model weights (~882 MB) download from HuggingFace on first use.

## Hardware Notes

- Intel Arc GPU available via `torch.xpu` after IPEX install
- 98 GB system RAM for shared memory fallback
- Model is 200M params, fits in 2 GB VRAM
- `force_flip_invariance=False` halves inference time
- `torch_compile=False` avoids startup compilation penalty

## Test Strategy

Unit tests should:
1. Mock the TimesFM model to avoid GPU/download dependency in CI
2. Test direction mapping: bullish when predicted > current + 0.5%
3. Test bearish when predicted < current - 0.5%
4. Test neutral in the dead zone
5. Test confidence scaling from quantile spread
6. Test that analyze() returns a valid BeliefSnapshot
7. Test lazy model loading (model not loaded until first analyze call)

## Integration

The source gets registered in the belief orchestrator alongside existing sources.
The consensus system already handles N sources via majority vote — adding one more
source just adds another vote. No changes needed to consensus.py.

## Out of scope

- Fine-tuning TimesFM on crypto data (future work)
- Multi-variate input (OHLCV) — close only for now
- Batch prediction across all pairs (optimize later if single-pair is too slow)
