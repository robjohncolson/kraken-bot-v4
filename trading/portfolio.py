from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import TypeAlias

from core.config import DEFAULT_GRID_MAKER_OFFSET_PCT
from core.errors import KrakenBotError
from core.types import (
    Action,
    BeliefDirection,
    BeliefSnapshot,
    OrderRequest,
    OrderSide,
    OrderType,
    PairAllocation,
    PlaceOrder,
    Portfolio,
    Position,
    PositionId,
    PositionSide,
    Price,
    Quantity,
    UsdAmount,
    ZERO_DECIMAL,
)

DOGE_USD_PAIR = "DOGE/USD"
PERCENT_BASE = Decimal("100")
DEFAULT_DOGE_MAKER_OFFSET_PCT = Decimal(str(DEFAULT_GRID_MAKER_OFFSET_PCT))

PortfolioCloseActions: TypeAlias = tuple[Action, ...]
PortfolioCloseResult: TypeAlias = tuple[Portfolio, PortfolioCloseActions]


class PortfolioManagerError(KrakenBotError):
    """Base exception for pure portfolio transitions."""


class InvalidPositionStateError(PortfolioManagerError):
    """Raised when a position cannot be valued for accounting."""

    def __init__(self, position_id: str, field_name: str, raw_value: Decimal) -> None:
        self.position_id = position_id
        self.field_name = field_name
        self.raw_value = raw_value
        super().__init__(
            f"Position {position_id!r} has invalid {field_name} value {raw_value}."
        )


class PortfolioPositionNotFoundError(PortfolioManagerError):
    """Raised when a close references a missing position."""

    def __init__(self, position_id: str) -> None:
        self.position_id = position_id
        super().__init__(f"Position {position_id!r} is not present in the portfolio.")


class InvalidClosePriceError(PortfolioManagerError):
    """Raised when a close price is zero or negative."""

    def __init__(self, close_price: Price) -> None:
        self.close_price = close_price
        super().__init__(f"close_price must be positive; got {close_price}.")


class InvalidDogeBeliefError(PortfolioManagerError):
    """Raised when DOGE accumulation receives the wrong belief snapshot."""

    def __init__(self, pair: str) -> None:
        self.pair = pair
        super().__init__(f"DOGE accumulation requires a {DOGE_USD_PAIR!r} belief; got {pair!r}.")


class MissingDogeMarketPriceError(PortfolioManagerError):
    """Raised when DOGE accumulation cannot price the maker order."""

    def __init__(self) -> None:
        super().__init__("doge_market_price is required when DOGE accumulation is bullish.")


class InvalidDogeMarketPriceError(PortfolioManagerError):
    """Raised when DOGE accumulation receives a non-positive market price."""

    def __init__(self, doge_market_price: Price) -> None:
        self.doge_market_price = doge_market_price
        super().__init__(f"doge_market_price must be positive; got {doge_market_price}.")


class InvalidMakerOffsetError(PortfolioManagerError):
    """Raised when the maker offset would produce an invalid limit price."""

    def __init__(self, maker_offset_pct: Decimal) -> None:
        self.maker_offset_pct = maker_offset_pct
        super().__init__(
            "maker_offset_pct must be greater than or equal to 0 and less than 100; "
            f"got {maker_offset_pct}."
        )


class PortfolioManager:
    """Pure portfolio accounting in USD with optional DOGE accumulation actions."""

    @staticmethod
    def apply_fill(
        portfolio: Portfolio,
        *,
        position: Position,
    ) -> Portfolio:
        _validate_position(position)
        current_position, current_index = _locate_position(portfolio.positions, position.position_id)
        current_value = ZERO_DECIMAL if current_position is None else _signed_position_value_usd(current_position)
        new_value = _signed_position_value_usd(position)
        cash_usd = portfolio.cash_usd - (new_value - current_value)

        positions = list(portfolio.positions)
        if current_index is None:
            positions.append(position)
        else:
            positions[current_index] = position

        return _rebalance_portfolio(
            portfolio,
            cash_usd=cash_usd,
            positions=tuple(positions),
        )

    @staticmethod
    def apply_close(
        portfolio: Portfolio,
        *,
        position_id: PositionId,
        close_price: Price,
        doge_belief: BeliefSnapshot | None = None,
        doge_market_price: Price | None = None,
        maker_offset_pct: Decimal = DEFAULT_DOGE_MAKER_OFFSET_PCT,
    ) -> PortfolioCloseResult:
        if close_price <= ZERO_DECIMAL:
            raise InvalidClosePriceError(close_price)

        position, index = _locate_position(portfolio.positions, position_id)
        if position is None or index is None:
            raise PortfolioPositionNotFoundError(position_id)

        close_value = _signed_position_value_usd(position, price=close_price)
        realized_pnl_usd = close_value - _signed_position_value_usd(position)
        remaining_positions = (
            portfolio.positions[:index] + portfolio.positions[index + 1 :]
        )
        updated_portfolio = _rebalance_portfolio(
            portfolio,
            cash_usd=portfolio.cash_usd + close_value,
            positions=remaining_positions,
        )

        actions: PortfolioCloseActions = ()
        if _is_bullish_doge(doge_belief) and realized_pnl_usd > ZERO_DECIMAL:
            if doge_market_price is None:
                raise MissingDogeMarketPriceError()
            if doge_market_price <= ZERO_DECIMAL:
                raise InvalidDogeMarketPriceError(doge_market_price)
            limit_price = _doge_limit_price(doge_market_price, maker_offset_pct)
            actions = (
                PlaceOrder(
                    order=OrderRequest(
                        pair=DOGE_USD_PAIR,
                        side=OrderSide.BUY,
                        order_type=OrderType.LIMIT,
                        quantity=realized_pnl_usd / limit_price,
                        limit_price=limit_price,
                    )
                ),
            )

        return updated_portfolio, actions

    @staticmethod
    def compute_concentration(portfolio: Portfolio) -> tuple[PairAllocation, ...]:
        total_value_usd = _total_value_usd(portfolio.cash_usd, portfolio.positions)
        if total_value_usd <= ZERO_DECIMAL:
            return ()

        pair_totals: dict[str, Decimal] = {}
        for position in portfolio.positions:
            notional_usd = _absolute_position_value_usd(position)
            pair_totals[position.pair] = pair_totals.get(position.pair, ZERO_DECIMAL) + notional_usd

        return tuple(
            PairAllocation(pair=pair, percent=notional_usd / total_value_usd)
            for pair, notional_usd in sorted(pair_totals.items())
        )

    @staticmethod
    def compute_directional_exposure(portfolio: Portfolio) -> Decimal:
        total_value_usd = _total_value_usd(portfolio.cash_usd, portfolio.positions)
        if total_value_usd <= ZERO_DECIMAL:
            return ZERO_DECIMAL
        net_directional_value = sum(
            (_signed_position_value_usd(position) for position in portfolio.positions),
            start=ZERO_DECIMAL,
        )
        return net_directional_value / total_value_usd


def apply_fill(
    portfolio: Portfolio,
    *,
    position: Position,
) -> Portfolio:
    return PortfolioManager.apply_fill(portfolio, position=position)


def apply_close(
    portfolio: Portfolio,
    *,
    position_id: PositionId,
    close_price: Price,
    doge_belief: BeliefSnapshot | None = None,
    doge_market_price: Price | None = None,
    maker_offset_pct: Decimal = DEFAULT_DOGE_MAKER_OFFSET_PCT,
) -> PortfolioCloseResult:
    return PortfolioManager.apply_close(
        portfolio,
        position_id=position_id,
        close_price=close_price,
        doge_belief=doge_belief,
        doge_market_price=doge_market_price,
        maker_offset_pct=maker_offset_pct,
    )


def compute_concentration(portfolio: Portfolio) -> tuple[PairAllocation, ...]:
    return PortfolioManager.compute_concentration(portfolio)


def compute_directional_exposure(portfolio: Portfolio) -> Decimal:
    return PortfolioManager.compute_directional_exposure(portfolio)


def _locate_position(
    positions: tuple[Position, ...],
    position_id: PositionId,
) -> tuple[Position | None, int | None]:
    for index, position in enumerate(positions):
        if position.position_id == position_id:
            return position, index
    return None, None


def mark_to_market(
    portfolio: Portfolio,
    *,
    doge_price_usd: Price = ZERO_DECIMAL,
) -> Portfolio:
    """Recompute total_value_usd, concentration, exposure with current DOGE price."""
    return _rebalance_portfolio(
        portfolio,
        cash_usd=portfolio.cash_usd,
        positions=portfolio.positions,
        doge_price_usd=doge_price_usd,
    )


def _rebalance_portfolio(
    portfolio: Portfolio,
    *,
    cash_usd: UsdAmount,
    positions: tuple[Position, ...],
    doge_price_usd: Price = ZERO_DECIMAL,
) -> Portfolio:
    snapshot = replace(
        portfolio,
        cash_usd=cash_usd,
        positions=positions,
        total_value_usd=_total_value_usd(
            cash_usd, positions,
            cash_doge=portfolio.cash_doge, doge_price_usd=doge_price_usd,
        ),
    )
    concentration = compute_concentration(snapshot)
    directional_exposure = compute_directional_exposure(snapshot)
    return replace(
        snapshot,
        concentration=concentration,
        directional_exposure=directional_exposure,
    )


def _total_value_usd(
    cash_usd: UsdAmount,
    positions: tuple[Position, ...],
    cash_doge: Quantity = ZERO_DECIMAL,
    doge_price_usd: Price = ZERO_DECIMAL,
) -> UsdAmount:
    doge_value = cash_doge * doge_price_usd if doge_price_usd > ZERO_DECIMAL else ZERO_DECIMAL
    return cash_usd + doge_value + sum(
        (_signed_position_value_usd(position) for position in positions),
        start=ZERO_DECIMAL,
    )


def _signed_position_value_usd(position: Position, *, price: Price | None = None) -> Decimal:
    notional_usd = _absolute_position_value_usd(position, price=price)
    if position.side == PositionSide.SHORT:
        return -notional_usd
    return notional_usd


def _absolute_position_value_usd(position: Position, *, price: Price | None = None) -> Decimal:
    _validate_position(position)
    valuation_price = position.entry_price if price is None else price
    if valuation_price <= ZERO_DECIMAL:
        raise InvalidPositionStateError(position.position_id, "price", valuation_price)
    return position.quantity * valuation_price


def _validate_position(position: Position) -> None:
    if position.quantity <= ZERO_DECIMAL:
        raise InvalidPositionStateError(position.position_id, "quantity", position.quantity)
    if position.entry_price <= ZERO_DECIMAL:
        raise InvalidPositionStateError(position.position_id, "entry_price", position.entry_price)


def _is_bullish_doge(doge_belief: BeliefSnapshot | None) -> bool:
    if doge_belief is None:
        return False
    if doge_belief.pair != DOGE_USD_PAIR:
        raise InvalidDogeBeliefError(doge_belief.pair)
    return doge_belief.direction == BeliefDirection.BULLISH


def _doge_limit_price(doge_market_price: Price, maker_offset_pct: Decimal) -> Price:
    if maker_offset_pct < ZERO_DECIMAL or maker_offset_pct >= PERCENT_BASE:
        raise InvalidMakerOffsetError(maker_offset_pct)
    return doge_market_price * ((PERCENT_BASE - maker_offset_pct) / PERCENT_BASE)


__all__ = [
    "apply_close",
    "apply_fill",
    "compute_concentration",
    "compute_directional_exposure",
    "DEFAULT_DOGE_MAKER_OFFSET_PCT",
    "DOGE_USD_PAIR",
    "InvalidClosePriceError",
    "InvalidDogeBeliefError",
    "InvalidDogeMarketPriceError",
    "InvalidMakerOffsetError",
    "InvalidPositionStateError",
    "mark_to_market",
    "MissingDogeMarketPriceError",
    "PortfolioCloseActions",
    "PortfolioCloseResult",
    "PortfolioManager",
    "PortfolioManagerError",
    "PortfolioPositionNotFoundError",
]
