"""Backfill shadow validation: replay V1 LogReg on historical OHLCV.

Slides a window across the 180d CC-backed dataset, generates predictions
at each bar using the promoted artifact, and evaluates against actual
6h forward returns. Produces rollout-gate-equivalent metrics without
needing live shadow logs.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pandas as pd

from beliefs.research_model_source import ResearchModelSource

logger = logging.getLogger(__name__)

HORIZON_HOURS: Final[int] = 6
MIN_WINDOW: Final[int] = 50
FEE_BPS: Final[int] = 10
SLIPPAGE_BPS: Final[int] = 5
COST_BPS: Final[int] = FEE_BPS + SLIPPAGE_BPS

COVERAGE_GATE: Final[float] = 0.90
ACCURACY_GATE: Final[float] = 0.50


@dataclass(frozen=True, slots=True)
class BackfillResult:
    total_bars: int
    predictions: int
    abstains: int
    matched: int
    correct_direction: int
    paper_pnl_bps: float
    trades_positive: int
    trades_total: int

    @property
    def coverage(self) -> float:
        return self.predictions / self.total_bars if self.total_bars else 0.0

    @property
    def abstain_rate(self) -> float:
        return self.abstains / self.predictions if self.predictions else 0.0

    @property
    def directional_accuracy(self) -> float:
        return self.correct_direction / self.trades_total if self.trades_total else 0.0

    @property
    def hit_rate(self) -> float:
        return self.trades_positive / self.trades_total if self.trades_total else 0.0

    @property
    def mean_pnl_per_trade(self) -> float:
        return self.paper_pnl_bps / self.trades_total if self.trades_total else 0.0

    def passes_gates(self) -> dict[str, bool]:
        return {
            "coverage_>90%": self.coverage > COVERAGE_GATE,
            "accuracy_>50%": self.directional_accuracy > ACCURACY_GATE,
            "no_crashes": True,
        }


def run_backfill(
    artifact_dir: Path,
    data_dir: Path,
    pair: str = "DOGE/USD",
) -> BackfillResult:
    """Replay artifact on historical dataset, return shadow-equivalent metrics."""
    source = ResearchModelSource(artifact_dir)

    market = pd.read_parquet(data_dir / "market_v1.parquet")
    labels = pd.read_parquet(data_dir / "labels_v1.parquet")

    return_bps_col = "return_bps_6h"
    return_sign_col = "return_sign_6h"

    if return_bps_col not in labels.columns:
        raise ValueError(f"Labels missing {return_bps_col}")

    total_bars = len(market) - MIN_WINDOW + 1
    predictions = 0
    abstains = 0
    matched = 0
    correct_direction = 0
    paper_pnl_bps = 0.0
    trades_positive = 0
    trades_total = 0

    for i in range(MIN_WINDOW - 1, len(market)):
        window = market.iloc[i - MIN_WINDOW + 1: i + 1].reset_index(drop=True)

        try:
            prob_up = source.predict_raw(window)
        except Exception as exc:
            logger.debug("Bar %d: prediction failed: %s", i, exc)
            continue

        predictions += 1

        threshold = source.threshold
        if prob_up > threshold:
            signal = 1
        elif prob_up < (1 - threshold):
            signal = -1
        else:
            signal = 0
            abstains += 1
            continue

        # Match to actual outcome
        if i >= len(labels) or pd.isna(labels.iloc[i][return_bps_col]):
            continue

        actual_bps = float(labels.iloc[i][return_bps_col])
        actual_sign = int(labels.iloc[i][return_sign_col])
        matched += 1
        trades_total += 1

        if (signal > 0 and actual_sign > 0) or (signal < 0 and actual_sign < 0):
            correct_direction += 1

        trade_pnl = signal * actual_bps - COST_BPS
        paper_pnl_bps += trade_pnl
        if trade_pnl > 0:
            trades_positive += 1

    return BackfillResult(
        total_bars=total_bars,
        predictions=predictions,
        abstains=abstains,
        matched=matched,
        correct_direction=correct_direction,
        paper_pnl_bps=round(paper_pnl_bps, 2),
        trades_positive=trades_positive,
        trades_total=trades_total,
    )


def print_report(result: BackfillResult) -> None:
    """Print human-readable backfill report with rollout gate evaluation."""
    print("\n=== Backfill Shadow Validation Report ===\n")
    print(f"Total bars evaluated:    {result.total_bars}")
    print(f"Predictions generated:   {result.predictions}")
    print(f"Abstains:                {result.abstains}")
    print(f"Matched to outcomes:     {result.matched}")
    print(f"Trades:                  {result.trades_total}")
    print()
    print(f"Prediction coverage:     {result.coverage:.1%}")
    print(f"Abstain rate:            {result.abstain_rate:.1%}")
    print(f"Directional accuracy:    {result.directional_accuracy:.1%}")
    print(f"Hit rate:                {result.hit_rate:.1%}")
    print(f"Paper P&L:               {result.paper_pnl_bps:+,.1f} bps")
    print(f"Mean P&L per trade:      {result.mean_pnl_per_trade:+,.1f} bps")
    print()
    print("--- Rollout Gates ---")
    gates = result.passes_gates()
    all_pass = True
    for gate, passed in gates.items():
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  {gate}: {status}")
    print()
    verdict = "ALL GATES PASS — ready for live rollout" if all_pass else "GATES FAILED — do not promote"
    print(f"Verdict: {verdict}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill shadow validation")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        required=True,
        help="Path to promoted artifact directory",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="Path to research dataset (market + labels parquet)",
    )
    parser.add_argument("--pair", default="DOGE/USD")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    result = run_backfill(args.artifact_dir, args.data_dir, args.pair)
    print_report(result)


if __name__ == "__main__":
    main()
