from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TypeAlias

from core.config import Settings
from core.types import ClosePosition, Portfolio, Position, PositionSide, ZERO_DECIMAL
from trading.sizing import size_position_usd

PERCENT_BASE = Decimal("100")
NO_ACTION_REASON = "none"
SOFT_DRAWDOWN_REASON = "max_drawdown_soft_limit"
HARD_DRAWDOWN_REASON = "max_drawdown_hard_limit"


@dataclass(frozen=True, slots=True)
class MissingStopLossViolation:
    position_id: str
    pair: str


@dataclass(frozen=True, slots=True)
class KellySizeViolation:
    position_id: str
    pair: str
    size_usd: Decimal
    max_size_usd: Decimal


@dataclass(frozen=True, slots=True)
class ReentryCooldownViolation:
    pair: str
    last_stop_loss_at: datetime
    blocked_until: datetime


@dataclass(frozen=True, slots=True)
class MaxPositionsViolation:
    open_positions: int
    max_positions: int


@dataclass(frozen=True, slots=True)
class SameSideConcentrationViolation:
    side: PositionSide
    concentration: Decimal
    limit: Decimal


@dataclass(frozen=True, slots=True)
class SinglePairAllocationViolation:
    pair: str
    allocation: Decimal
    limit: Decimal


@dataclass(frozen=True, slots=True)
class SoftDrawdownViolation:
    drawdown: Decimal
    limit: Decimal


@dataclass(frozen=True, slots=True)
class HardDrawdownViolation:
    drawdown: Decimal
    limit: Decimal


RiskViolation: TypeAlias = (
    MissingStopLossViolation
    | KellySizeViolation
    | ReentryCooldownViolation
    | MaxPositionsViolation
    | SameSideConcentrationViolation
    | SinglePairAllocationViolation
    | SoftDrawdownViolation
    | HardDrawdownViolation
)


@dataclass(frozen=True, slots=True)
class NoAction:
    reason: str = NO_ACTION_REASON


@dataclass(frozen=True, slots=True)
class BlockNewEntries:
    reason: str = SOFT_DRAWDOWN_REASON


@dataclass(frozen=True, slots=True)
class CloseAllPositions:
    actions: tuple[ClosePosition, ...]


RiskRecommendedAction: TypeAlias = NoAction | BlockNewEntries | CloseAllPositions


@dataclass(frozen=True, slots=True)
class RiskCheckResult:
    passed: bool
    violations: list[RiskViolation]
    recommended_action: RiskRecommendedAction


def check_position_rules(
    position: Position,
    *,
    portfolio_value_usd: Decimal,
    kelly_fraction_value: Decimal,
    config: Settings,
    as_of: datetime | None = None,
    last_stop_loss_at: datetime | None = None,
) -> RiskCheckResult:
    violations: list[RiskViolation] = []

    if position.stop_price <= ZERO_DECIMAL:
        violations.append(
            MissingStopLossViolation(position_id=position.position_id, pair=position.pair)
        )

    max_size_usd = size_position_usd(
        portfolio_value_usd=portfolio_value_usd,
        kelly_fraction_value=kelly_fraction_value,
        min_position_usd=Decimal(config.min_position_usd),
        max_position_usd=Decimal(config.max_position_usd),
    )
    position_size_usd = _position_notional_usd(position)
    if position_size_usd > max_size_usd:
        violations.append(
            KellySizeViolation(
                position_id=position.position_id,
                pair=position.pair,
                size_usd=position_size_usd,
                max_size_usd=max_size_usd,
            )
        )

    if last_stop_loss_at is not None and as_of is not None:
        blocked_until = last_stop_loss_at + timedelta(hours=config.reentry_cooldown_hours)
        if as_of < blocked_until:
            violations.append(
                ReentryCooldownViolation(
                    pair=position.pair,
                    last_stop_loss_at=last_stop_loss_at,
                    blocked_until=blocked_until,
                )
            )

    return _result(violations)


def check_portfolio_rules(
    portfolio: Portfolio,
    *,
    config: Settings,
) -> RiskCheckResult:
    violations: list[RiskViolation] = []
    recommended_action: RiskRecommendedAction = NoAction()
    total_value_usd = _portfolio_total_value_usd(portfolio)

    if len(portfolio.positions) > config.max_positions:
        violations.append(
            MaxPositionsViolation(
                open_positions=len(portfolio.positions),
                max_positions=config.max_positions,
            )
        )

    same_side_limit = _ratio(config.max_same_side_pct)
    for side, concentration in _same_side_concentrations(portfolio, total_value_usd):
        if concentration > same_side_limit:
            violations.append(
                SameSideConcentrationViolation(
                    side=side,
                    concentration=concentration,
                    limit=same_side_limit,
                )
            )

    pair_limit = _ratio(config.max_single_pair_pct)
    for pair, allocation in _pair_allocations(portfolio, total_value_usd):
        if allocation > pair_limit:
            violations.append(
                SinglePairAllocationViolation(pair=pair, allocation=allocation, limit=pair_limit)
            )

    hard_drawdown_limit = _ratio(config.max_drawdown_hard_pct)
    soft_drawdown_limit = _ratio(config.max_drawdown_soft_pct)
    if portfolio.max_drawdown >= hard_drawdown_limit:
        violations.append(
            HardDrawdownViolation(
                drawdown=portfolio.max_drawdown,
                limit=hard_drawdown_limit,
            )
        )
        recommended_action = CloseAllPositions(
            actions=tuple(
                ClosePosition(
                    position_id=position.position_id,
                    reason=HARD_DRAWDOWN_REASON,
                )
                for position in portfolio.positions
            )
        )
    elif portfolio.max_drawdown >= soft_drawdown_limit:
        violations.append(
            SoftDrawdownViolation(
                drawdown=portfolio.max_drawdown,
                limit=soft_drawdown_limit,
            )
        )
        recommended_action = BlockNewEntries()

    return _result(violations, recommended_action=recommended_action)


def _result(
    violations: list[RiskViolation],
    *,
    recommended_action: RiskRecommendedAction | None = None,
) -> RiskCheckResult:
    action = NoAction() if recommended_action is None else recommended_action
    return RiskCheckResult(
        passed=not violations,
        violations=list(violations),
        recommended_action=action,
    )


def _position_notional_usd(position: Position) -> Decimal:
    return position.quantity * position.entry_price


def _portfolio_total_value_usd(portfolio: Portfolio) -> Decimal:
    return portfolio.cash_usd + sum(
        (_signed_notional_usd(position) for position in portfolio.positions),
        start=ZERO_DECIMAL,
    )


def _signed_notional_usd(position: Position) -> Decimal:
    notional_usd = _position_notional_usd(position)
    if position.side == PositionSide.SHORT:
        return -notional_usd
    return notional_usd


def _same_side_concentrations(
    portfolio: Portfolio,
    total_value_usd: Decimal,
) -> tuple[tuple[PositionSide, Decimal], ...]:
    if total_value_usd <= ZERO_DECIMAL:
        return ()

    long_total = ZERO_DECIMAL
    short_total = ZERO_DECIMAL
    for position in portfolio.positions:
        if position.side == PositionSide.LONG:
            long_total += _position_notional_usd(position)
        else:
            short_total += _position_notional_usd(position)

    return (
        (PositionSide.LONG, long_total / total_value_usd),
        (PositionSide.SHORT, short_total / total_value_usd),
    )


def _pair_allocations(
    portfolio: Portfolio,
    total_value_usd: Decimal,
) -> tuple[tuple[str, Decimal], ...]:
    if total_value_usd <= ZERO_DECIMAL:
        return ()

    pair_totals: dict[str, Decimal] = {}
    for position in portfolio.positions:
        pair_totals[position.pair] = pair_totals.get(position.pair, ZERO_DECIMAL) + (
            _position_notional_usd(position)
        )

    return tuple(
        (pair, notional_usd / total_value_usd)
        for pair, notional_usd in sorted(pair_totals.items())
    )


def _ratio(raw_percent: int) -> Decimal:
    return Decimal(raw_percent) / PERCENT_BASE


__all__ = [
    "BlockNewEntries",
    "check_portfolio_rules",
    "check_position_rules",
    "CloseAllPositions",
    "HardDrawdownViolation",
    "HARD_DRAWDOWN_REASON",
    "KellySizeViolation",
    "MaxPositionsViolation",
    "MissingStopLossViolation",
    "NoAction",
    "NO_ACTION_REASON",
    "ReentryCooldownViolation",
    "RiskCheckResult",
    "RiskRecommendedAction",
    "RiskViolation",
    "SameSideConcentrationViolation",
    "SinglePairAllocationViolation",
    "SoftDrawdownViolation",
    "SOFT_DRAWDOWN_REASON",
]
