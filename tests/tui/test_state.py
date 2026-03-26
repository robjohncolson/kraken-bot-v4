"""Unit tests for tui.state — parsers, merge, ring buffer."""
from __future__ import annotations

from tui.state import (
    CockpitState,
    PortfolioState,
    merge_sse_update,
    parse_beliefs,
    parse_health,
    parse_orders,
    parse_portfolio,
    parse_positions,
    parse_reconciliation,
)


# -- parse_health -------------------------------------------------------------

class TestParseHealth:
    def test_basic(self) -> None:
        raw = {
            "version": "0.1.0",
            "uptime_seconds": 123.4,
            "phase_status": {"id": "5", "name": "Observability", "status": "in_progress"},
        }
        h = parse_health(raw)
        assert h.version == "0.1.0"
        assert h.uptime_seconds == 123.4
        assert h.phase_name == "Observability"
        assert h.phase_status == "in_progress"

    def test_missing_fields(self) -> None:
        h = parse_health({})
        assert h.version == ""
        assert h.uptime_seconds == 0.0
        assert h.phase_name == ""


# -- parse_portfolio -----------------------------------------------------------

class TestParsePortfolio:
    def test_basic(self) -> None:
        raw = {"cash_usd": "142.50", "cash_doge": "1234", "total_value_usd": "185"}
        p = parse_portfolio(raw)
        assert p.cash_usd == "142.50"
        assert p.cash_doge == "1234"
        assert p.total_value_usd == "185"

    def test_defaults(self) -> None:
        p = parse_portfolio({})
        assert p.cash_usd == "0"


# -- parse_positions -----------------------------------------------------------

class TestParsePositions:
    def test_nested_position(self) -> None:
        raw = {
            "positions": [
                {
                    "position": {
                        "pair": "DOGE/USD",
                        "side": "long",
                        "quantity": "500",
                        "entry_price": "0.085",
                        "stop_price": "0.075",
                        "target_price": "0.110",
                        "grid_state": {"phase": "s0"},
                    },
                    "current_price": "0.092",
                    "unrealized_pnl_usd": "3.50",
                }
            ]
        }
        rows = parse_positions(raw)
        assert len(rows) == 1
        assert rows[0].pair == "DOGE/USD"
        assert rows[0].current_price == "0.092"
        assert rows[0].unrealized_pnl == "3.50"
        assert rows[0].grid_phase == "s0"

    def test_empty(self) -> None:
        assert parse_positions({}) == []
        assert parse_positions({"positions": []}) == []


# -- parse_beliefs -------------------------------------------------------------

class TestParseBeliefs:
    def test_grouped_dict(self) -> None:
        raw = {
            "beliefs": {
                "DOGE/USD": {
                    "technical_ensemble": {
                        "direction": "bullish",
                        "confidence": 0.72,
                        "regime": "ranging",
                    }
                }
            }
        }
        cells = parse_beliefs(raw)
        assert len(cells) == 1
        assert cells[0].pair == "DOGE/USD"
        assert cells[0].source == "technical_ensemble"
        assert cells[0].direction == "bullish"
        assert cells[0].confidence == 0.72

    def test_flat_list(self) -> None:
        raw = {
            "beliefs": [
                {
                    "pair": "DOGE/USD",
                    "source": "technical_ensemble",
                    "direction": "bearish",
                    "confidence": 0.45,
                    "regime": "unknown",
                }
            ]
        }
        cells = parse_beliefs(raw)
        assert len(cells) == 1
        assert cells[0].direction == "bearish"

    def test_empty(self) -> None:
        assert parse_beliefs({}) == []
        assert parse_beliefs({"beliefs": {}}) == []
        assert parse_beliefs({"beliefs": []}) == []


# -- parse_orders --------------------------------------------------------------

class TestParseOrders:
    def test_pending_orders(self) -> None:
        raw = {
            "pending_orders": [
                {"pair": "DOGE/USD", "side": "buy", "base_qty": "500", "filled_qty": "0", "client_order_id": "abc", "kind": "position_entry"}
            ]
        }
        rows = parse_orders(raw)
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert rows[0].kind == "position_entry"

    def test_open_orders(self) -> None:
        raw = {
            "open_orders": [
                {"order_id": "XYZ", "pair": "DOGE/USD", "side": "sell", "order_type": "limit", "status": "open", "quantity": "500", "filled_quantity": "100"}
            ]
        }
        rows = parse_orders(raw)
        assert len(rows) == 1
        assert rows[0].order_id == "XYZ"


# -- parse_reconciliation -----------------------------------------------------

class TestParseReconciliation:
    def test_flat_format(self) -> None:
        raw = {
            "checked_at": "2025-01-01T00:00:00",
            "discrepancy_detected": True,
            "ghost_positions": ["a"],
            "foreign_orders": [],
            "fee_drift": [],
            "untracked_assets": ["b", "c"],
        }
        r = parse_reconciliation(raw)
        assert r.discrepancy_detected is True
        assert len(r.ghost_positions) == 1
        assert len(r.untracked_assets) == 2

    def test_nested_report_format(self) -> None:
        raw = {
            "checked_at": "2025-01-01T00:00:00",
            "report": {
                "discrepancy_detected": False,
                "ghost_positions": [],
                "foreign_orders": [],
                "fee_drift": [],
                "untracked_assets": [],
            },
        }
        r = parse_reconciliation(raw)
        assert r.discrepancy_detected is False

    def test_empty(self) -> None:
        r = parse_reconciliation({})
        assert r.discrepancy_detected is False


# -- CockpitState ring buffer --------------------------------------------------

class TestCockpitStateRingBuffer:
    def test_add_event(self) -> None:
        state = CockpitState()
        state.add_event("hello")
        assert state.events == ["hello"]

    def test_ring_buffer_cap(self) -> None:
        state = CockpitState()
        for i in range(300):
            state.add_event(f"event-{i}")
        assert len(state.events) == 200
        assert state.events[0] == "event-100"
        assert state.events[-1] == "event-299"


# -- merge_sse_update ----------------------------------------------------------

class TestMergeSSEUpdate:
    def test_portfolio_update(self) -> None:
        state = CockpitState()
        merge_sse_update(state, {
            "portfolio": {"cash_usd": "999", "total_value_usd": "1234"},
        })
        assert state.portfolio.cash_usd == "999"
        assert state.portfolio.total_value_usd == "1234"

    def test_positions_update(self) -> None:
        state = CockpitState()
        merge_sse_update(state, {
            "positions": {
                "positions": [
                    {
                        "position": {"pair": "DOGE/USD", "side": "long", "quantity": "100",
                                     "entry_price": "0.1", "stop_price": "0.09", "target_price": "0.12"},
                        "current_price": "0.11",
                        "unrealized_pnl_usd": "1.00",
                    }
                ]
            },
        })
        assert len(state.positions) == 1
        assert state.positions[0].pair == "DOGE/USD"

    def test_beliefs_update(self) -> None:
        state = CockpitState()
        merge_sse_update(state, {
            "beliefs": [
                {"pair": "DOGE/USD", "source": "technical_ensemble", "direction": "bullish", "confidence": 0.8, "regime": "trending"}
            ],
        })
        assert len(state.beliefs) == 1
        assert state.beliefs[0].confidence == 0.8

    def test_partial_update(self) -> None:
        state = CockpitState()
        state.portfolio = PortfolioState(cash_usd="50")
        merge_sse_update(state, {"reconciliation": {"discrepancy_detected": True, "ghost_positions": ["x"]}})
        # portfolio unchanged
        assert state.portfolio.cash_usd == "50"
        assert state.reconciliation.discrepancy_detected is True
