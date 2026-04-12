from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.types import (
    BeliefDirection,
    BeliefSource,
    GridPhase,
    GridState,
    MarketRegime,
    PairAllocation,
    Portfolio,
    Position,
    PositionSide,
)
from trading.reconciler import (
    GhostPosition,
    ReconciliationAction,
    ReconciliationReport,
    ReconciliationSeverity,
    UntrackedAsset,
)
from web.routes import (
    BeliefEntry,
    DashboardState,
    GridCycleSnapshot,
    GridPhaseCount,
    GridStatusSnapshot,
    PositionSnapshot,
    ReconciliationSnapshot,
    StrategyStatsSnapshot,
    create_cc_router,
    create_router,
)

AS_OF = datetime(2026, 3, 24, 16, 0, 0)


def _client(state: DashboardState) -> TestClient:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(create_router(state_provider=lambda: state))
    return TestClient(app)


def _cc_client(*, executor: object, db_conn: object) -> TestClient:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(
        create_cc_router(
            state_provider=_dashboard_state,
            executor=executor,
            db_conn=db_conn,
        )
    )
    return TestClient(app)


def _dashboard_state() -> DashboardState:
    position = Position(
        position_id="pos-1",
        pair="BTC/USD",
        side=PositionSide.LONG,
        quantity=Decimal("2"),
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        target_price=Decimal("120"),
        grid_state=GridState(
            phase=GridPhase.S1A,
            active_slot_count=2,
            accepting_new_entries=True,
            realized_pnl_usd=Decimal("5"),
        ),
    )
    portfolio = Portfolio(
        cash_usd=Decimal("800"),
        positions=(position,),
        total_value_usd=Decimal("1000"),
        concentration=(PairAllocation(pair="BTC/USD", percent=Decimal("0.2")),),
        directional_exposure=Decimal("0.2"),
        max_drawdown=Decimal("0.05"),
    )
    return DashboardState(
        portfolio=portfolio,
        positions=(
            PositionSnapshot(
                position=position,
                current_price=Decimal("110"),
                unrealized_pnl_usd=Decimal("20"),
            ),
        ),
        grids=(
            GridStatusSnapshot(
                pair="BTC/USD",
                active_slots=3,
                phase_distribution=(
                    GridPhaseCount(phase=GridPhase.S0, active_slots=1),
                    GridPhaseCount(phase=GridPhase.S1A, active_slots=1),
                    GridPhaseCount(phase=GridPhase.S2, active_slots=1),
                ),
                cycle_history=(
                    GridCycleSnapshot(
                        cycle_id="cycle-1",
                        realized_pnl_usd=Decimal("12.5"),
                        completed_at=AS_OF,
                    ),
                ),
            ),
        ),
        beliefs=(
            BeliefEntry(
                pair="BTC/USD",
                source=BeliefSource.CLAUDE,
                direction=BeliefDirection.BULLISH,
                confidence=0.7,
                regime=MarketRegime.RANGING,
                updated_at=AS_OF,
            ),
            BeliefEntry(
                pair="BTC/USD",
                source=BeliefSource.CODEX,
                direction=BeliefDirection.BULLISH,
                confidence=0.8,
                regime=MarketRegime.RANGING,
                updated_at=AS_OF,
            ),
            BeliefEntry(
                pair="DOGE/USD",
                source=BeliefSource.TECHNICAL_ENSEMBLE,
                direction=BeliefDirection.NEUTRAL,
                confidence=0.55,
                regime=MarketRegime.UNKNOWN,
                updated_at=AS_OF,
            ),
        ),
        stats=StrategyStatsSnapshot(
            trade_count=42,
            win_rate=0.57,
            win_rate_ci_low=0.44,
            win_rate_ci_high=0.68,
            sharpe_ratio=1.2,
            updated_at=AS_OF,
        ),
        reconciliation=ReconciliationSnapshot(
            checked_at=AS_OF,
            report=ReconciliationReport(
                ghost_positions=(
                    GhostPosition(
                        position_id="pos-ghost",
                        pair="BTC/USD",
                        severity=ReconciliationSeverity.HIGH,
                        recommended_action=ReconciliationAction.ALERT,
                    ),
                ),
                untracked_assets=(
                    UntrackedAsset(
                        asset="DOGE",
                        available=Decimal("25"),
                        held=Decimal("0"),
                        severity=ReconciliationSeverity.LOW,
                        recommended_action=ReconciliationAction.AUTO_FIX,
                    ),
                ),
            ),
        ),
    )


def test_portfolio_endpoint_returns_current_portfolio_state() -> None:
    response = _client(_dashboard_state()).get("/api/portfolio")

    assert response.status_code == 200
    body = response.json()
    assert Decimal(str(body["cash_usd"])) == Decimal("800")
    assert Decimal(str(body["total_value_usd"])) == Decimal("1000")
    assert body["positions"][0]["pair"] == "BTC/USD"
    assert body["positions"][0]["grid_state"]["phase"] == "s1a"


def test_positions_endpoint_returns_open_positions_with_pnl() -> None:
    response = _client(_dashboard_state()).get("/api/positions")

    assert response.status_code == 200
    body = response.json()
    assert body["positions"][0]["position"]["position_id"] == "pos-1"
    assert Decimal(str(body["positions"][0]["current_price"])) == Decimal("110")
    assert Decimal(str(body["positions"][0]["unrealized_pnl_usd"])) == Decimal("20")


def test_grid_endpoint_returns_grid_status_for_pair() -> None:
    response = _client(_dashboard_state()).get("/api/grid/BTC/USD")

    assert response.status_code == 200
    body = response.json()
    assert body["pair"] == "BTC/USD"
    assert body["active_slots"] == 3
    assert body["phase_distribution"] == {"s0": 1, "s1a": 1, "s2": 1}
    assert body["cycle_history"][0]["cycle_id"] == "cycle-1"
    assert Decimal(str(body["cycle_history"][0]["realized_pnl_usd"])) == Decimal("12.5")


def test_beliefs_endpoint_returns_latest_beliefs_grouped_by_pair_and_source() -> None:
    response = _client(_dashboard_state()).get("/api/beliefs")

    assert response.status_code == 200
    body = response.json()
    assert body["beliefs"]["BTC/USD"]["claude"]["direction"] == "bullish"
    assert body["beliefs"]["BTC/USD"]["codex"]["confidence"] == 0.8
    assert body["beliefs"]["DOGE/USD"]["technical_ensemble"]["direction"] == "neutral"


def test_stats_endpoint_returns_placeholder_strategy_statistics() -> None:
    response = _client(_dashboard_state()).get("/api/stats")

    assert response.status_code == 200
    body = response.json()
    assert body["trade_count"] == 42
    assert body["win_rate"] == 0.57
    assert body["win_rate_ci_low"] == 0.44
    assert body["win_rate_ci_high"] == 0.68
    assert body["sharpe_ratio"] == 1.2
    assert body["updated_at"] == AS_OF.isoformat()


def test_reconciliation_endpoint_returns_last_report() -> None:
    response = _client(_dashboard_state()).get("/api/reconciliation")

    assert response.status_code == 200
    body = response.json()
    assert body["checked_at"] == AS_OF.isoformat()
    assert body["discrepancy_detected"] is True
    assert body["ghost_positions"][0]["position_id"] == "pos-ghost"
    assert body["untracked_assets"][0]["asset"] == "DOGE"


def test_routes_are_get_only() -> None:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(create_router(state_provider=_dashboard_state))

    route_methods = {
        route.path: route.methods
        for route in app.routes
        if route.path.startswith("/api/")
    }

    assert set(route_methods) == {
        "/api/portfolio",
        "/api/positions",
        "/api/grid/{pair:path}",
        "/api/beliefs",
        "/api/stats",
        "/api/reconciliation",
        "/api/rotation-tree",
    }
    for methods in route_methods.values():
        assert "GET" in methods
        assert "POST" not in methods
        assert "PUT" not in methods
        assert "DELETE" not in methods


def test_place_order_persists_to_sqlite() -> None:
    executor = SimpleNamespace(execute_order=Mock(return_value="tx-123"))
    writer = Mock()

    response = _cc_client(executor=executor, db_conn=writer).post(
        "/api/orders",
        json={
            "pair": "MON/USDT",
            "side": "buy",
            "order_type": "limit",
            "quantity": "12.5",
            "limit_price": "0.42",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"txid": "tx-123", "status": "placed", "pair": "MON/USDT"}
    writer.upsert_order.assert_called_once_with(
        order_id="tx-123",
        pair="MON/USDT",
        client_order_id="kbv4-cc-tx-123",
        kind="cc_api",
        side="buy",
        base_qty=Decimal("12.5"),
        filled_qty=Decimal("0"),
        quote_qty=Decimal("0"),
        limit_price=Decimal("0.42"),
        exchange_order_id="tx-123",
        rotation_node_id=None,
    )


def test_place_order_persists_warning_on_writer_failure() -> None:
    executor = SimpleNamespace(execute_order=Mock(return_value="tx-456"))
    writer = Mock()
    writer.upsert_order.side_effect = RuntimeError("sqlite unavailable")

    response = _cc_client(executor=executor, db_conn=writer).post(
        "/api/orders",
        json={
            "pair": "ALEO/USDT",
            "side": "buy",
            "order_type": "limit",
            "quantity": "5",
            "limit_price": "1.23",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "txid": "tx-456",
        "status": "placed",
        "pair": "ALEO/USDT",
        "warning": "Order placed on Kraken but failed to persist to SQLite.",
    }
