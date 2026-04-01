"""Tests for rotation tree execution helpers and reducer integration."""

from __future__ import annotations

from decimal import Decimal

from core.types import (
    BotState,
    FillConfirmed,
    LogEvent,
    OrderSide,
    PendingOrder,
    ZERO_DECIMAL,
)
from core.config import Settings, load_settings
from core.state_machine import reduce
from trading.rotation_tree import (
    destination_quantity,
    entry_base_quantity,
    exit_base_quantity,
    exit_proceeds,
)


# ---------------------------------------------------------------------------
# entry_base_quantity
# ---------------------------------------------------------------------------


class TestEntryBaseQuantity:
    def test_buy_converts_quote_to_base(self):
        # Parent has $300 USD, buying ETH at $3000 → 0.1 ETH
        result = entry_base_quantity(OrderSide.BUY, Decimal("300"), Decimal("3000"))
        assert result == Decimal("0.1")

    def test_sell_passes_base_through(self):
        # Parent has 1000 DOGE, selling DOGE on DOGE/USD → 1000 DOGE
        result = entry_base_quantity(OrderSide.SELL, Decimal("1000"), Decimal("0.17"))
        assert result == Decimal("1000")

    def test_buy_small_allocation(self):
        result = entry_base_quantity(OrderSide.BUY, Decimal("10"), Decimal("50000"))
        assert result == Decimal("0.0002")


# ---------------------------------------------------------------------------
# destination_quantity
# ---------------------------------------------------------------------------


class TestDestinationQuantity:
    def test_buy_returns_fill_qty(self):
        # BUY ETH/USD: filled 0.1 ETH at $3000 → child gets 0.1 ETH
        result = destination_quantity(OrderSide.BUY, Decimal("0.1"), Decimal("3000"))
        assert result == Decimal("0.1")

    def test_sell_returns_fill_times_price(self):
        # SELL DOGE/USD: filled 1000 DOGE at $0.17 → child gets $170
        result = destination_quantity(OrderSide.SELL, Decimal("1000"), Decimal("0.17"))
        assert result == Decimal("170.00")

    def test_sell_precision(self):
        result = destination_quantity(OrderSide.SELL, Decimal("500"), Decimal("0.155"))
        assert result == Decimal("77.500")


# ---------------------------------------------------------------------------
# exit_base_quantity
# ---------------------------------------------------------------------------


class TestExitBaseQuantity:
    def test_entry_buy_exit_sells_held_base(self):
        # Entry was BUY (hold 0.1 ETH), exit SELL → qty = 0.1
        result = exit_base_quantity(OrderSide.BUY, Decimal("0.1"), Decimal("3200"))
        assert result == Decimal("0.1")

    def test_entry_sell_exit_buys_base_back(self):
        # Entry was SELL (hold $170 USD), exit BUY on DOGE/USD at $0.18
        # → buy $170 / $0.18 = ~944.44 DOGE
        result = exit_base_quantity(OrderSide.SELL, Decimal("170"), Decimal("0.18"))
        expected = Decimal("170") / Decimal("0.18")
        assert result == expected


# ---------------------------------------------------------------------------
# exit_proceeds
# ---------------------------------------------------------------------------


class TestExitProceeds:
    def test_entry_buy_exit_sell_returns_quote(self):
        # Entry was BUY on ETH/USD (hold ETH). Exit SELL: sold 0.1 ETH at $3200
        # → proceeds = 0.1 * 3200 = $320 (quote = parent's USD denom)
        result = exit_proceeds(OrderSide.BUY, Decimal("0.1"), Decimal("3200"))
        assert result == Decimal("320.0")

    def test_entry_sell_exit_buy_returns_base(self):
        # Entry was SELL on DOGE/USD (hold USD). Exit BUY: bought 944 DOGE at $0.18
        # → proceeds = 944 (base = parent's DOGE denom)
        result = exit_proceeds(OrderSide.SELL, Decimal("944"), Decimal("0.18"))
        assert result == Decimal("944")


# ---------------------------------------------------------------------------
# Round-trip conservation
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_buy_roundtrip_profit(self):
        """BUY entry → price goes up → SELL exit → profit in parent denom."""
        allocated_usd = Decimal("300")
        entry_price = Decimal("3000")
        exit_price = Decimal("3300")

        # Entry
        base_qty = entry_base_quantity(OrderSide.BUY, allocated_usd, entry_price)
        assert base_qty == Decimal("0.1")
        dest_qty = destination_quantity(OrderSide.BUY, base_qty, entry_price)
        assert dest_qty == Decimal("0.1")

        # Exit
        exit_qty = exit_base_quantity(OrderSide.BUY, dest_qty, exit_price)
        assert exit_qty == Decimal("0.1")
        proceeds = exit_proceeds(OrderSide.BUY, exit_qty, exit_price)
        assert proceeds == Decimal("330.0")
        assert proceeds > allocated_usd  # profit

    def test_sell_roundtrip_profit(self):
        """SELL entry → price goes down → BUY exit → profit in parent denom."""
        allocated_doge = Decimal("1000")
        entry_price = Decimal("0.17")
        exit_price = Decimal("0.15")

        # Entry
        base_qty = entry_base_quantity(OrderSide.SELL, allocated_doge, entry_price)
        assert base_qty == Decimal("1000")
        dest_qty = destination_quantity(OrderSide.SELL, base_qty, entry_price)
        assert dest_qty == Decimal("170.00")

        # Exit: buy back DOGE at lower price
        exit_qty = exit_base_quantity(OrderSide.SELL, dest_qty, exit_price)
        # dest_qty=170 / 0.15 = ~1133 DOGE
        proceeds = exit_proceeds(OrderSide.SELL, exit_qty, exit_price)
        assert proceeds > allocated_doge  # got back more DOGE


# ---------------------------------------------------------------------------
# Reducer: rotation fill handler
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return load_settings({
        "KRAKEN_API_KEY": "key",
        "KRAKEN_API_SECRET": "secret",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_KEY": "supabase-key",
    })


class TestReducerRotationFill:
    def _state_with_rotation_pending(self) -> BotState:
        pending = PendingOrder(
            client_order_id="kbv4-rot-root-usd-eth-0-entry",
            kind="rotation_entry",
            pair="ETH/USD",
            side=OrderSide.BUY,
            base_qty=Decimal("0.1"),
            quote_qty=Decimal("300"),
            rotation_node_id="root-usd-eth-0",
        )
        return BotState(pending_orders=(pending,))

    def test_rotation_entry_fill_removes_pending(self):
        state = self._state_with_rotation_pending()
        fill = FillConfirmed(
            order_id="O-123",
            pair="ETH/USD",
            filled_quantity=Decimal("0.1"),
            fill_price=Decimal("3000"),
            client_order_id="kbv4-rot-root-usd-eth-0-entry",
        )
        config = _settings()
        new_state, actions = reduce(state, fill, config)
        assert len(new_state.pending_orders) == 0
        assert any(isinstance(a, LogEvent) and "rotation" in a.message for a in actions)

    def test_rotation_fill_does_not_create_position(self):
        state = self._state_with_rotation_pending()
        fill = FillConfirmed(
            order_id="O-123",
            pair="ETH/USD",
            filled_quantity=Decimal("0.1"),
            fill_price=Decimal("3000"),
            client_order_id="kbv4-rot-root-usd-eth-0-entry",
        )
        config = _settings()
        new_state, _ = reduce(state, fill, config)
        # No Position created — rotation tree handles its own accounting
        assert len(new_state.portfolio.positions) == 0

    def test_rotation_fill_does_not_modify_portfolio_cash(self):
        state = self._state_with_rotation_pending()
        fill = FillConfirmed(
            order_id="O-123",
            pair="ETH/USD",
            filled_quantity=Decimal("0.1"),
            fill_price=Decimal("3000"),
            client_order_id="kbv4-rot-root-usd-eth-0-entry",
        )
        config = _settings()
        new_state, _ = reduce(state, fill, config)
        assert new_state.portfolio.cash_usd == state.portfolio.cash_usd
        assert new_state.portfolio.cash_doge == state.portfolio.cash_doge

    def test_rotation_exit_fill_removes_pending(self):
        pending = PendingOrder(
            client_order_id="kbv4-rot-root-usd-eth-0-exit",
            kind="rotation_exit",
            pair="ETH/USD",
            side=OrderSide.SELL,
            base_qty=Decimal("0.1"),
            quote_qty=ZERO_DECIMAL,
            rotation_node_id="root-usd-eth-0",
        )
        state = BotState(pending_orders=(pending,))
        fill = FillConfirmed(
            order_id="O-456",
            pair="ETH/USD",
            filled_quantity=Decimal("0.1"),
            fill_price=Decimal("3200"),
            client_order_id="kbv4-rot-root-usd-eth-0-exit",
        )
        config = _settings()
        new_state, actions = reduce(state, fill, config)
        assert len(new_state.pending_orders) == 0

    def test_partial_rotation_fill_updates_filled_qty(self):
        state = self._state_with_rotation_pending()
        fill = FillConfirmed(
            order_id="O-123",
            pair="ETH/USD",
            filled_quantity=Decimal("0.05"),
            fill_price=Decimal("3000"),
            client_order_id="kbv4-rot-root-usd-eth-0-entry",
        )
        config = _settings()
        new_state, _ = reduce(state, fill, config)
        # Partial fill — PendingOrder should still be there with updated filled_qty
        assert len(new_state.pending_orders) == 1
        assert new_state.pending_orders[0].filled_qty == Decimal("0.05")
