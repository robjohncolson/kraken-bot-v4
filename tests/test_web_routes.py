from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.types import Portfolio
from web.routes import (
    DashboardState,
    RotationNodeSnapshot,
    RotationTreeSnapshot,
    create_cc_router,
    create_router,
)


def _root_node(*, node_id: str, asset: str, quantity_total: str) -> RotationNodeSnapshot:
    return RotationNodeSnapshot(
        node_id=node_id,
        parent_node_id=None,
        depth=0,
        asset=asset,
        quantity_total=quantity_total,
        quantity_free=quantity_total,
        quantity_reserved="0",
        status="open",
    )


def _rotation_tree(
    *,
    total_portfolio_value_usd: str,
    node_specs: tuple[tuple[str, str, str], ...] = (
        ("root-usd", "USD", "100"),
        ("root-ada", "ADA", "50"),
    ),
) -> RotationTreeSnapshot:
    nodes = tuple(
        _root_node(node_id=node_id, asset=asset, quantity_total=quantity_total)
        for node_id, asset, quantity_total in node_specs
    )
    return RotationTreeSnapshot(
        nodes=nodes,
        root_node_ids=tuple(node.node_id for node in nodes),
        rotation_tree_value_usd=total_portfolio_value_usd,
        total_portfolio_value_usd=total_portfolio_value_usd,
    )


def _balances_client(
    state: DashboardState,
    *,
    include_dashboard_routes: bool = False,
) -> TestClient:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def state_provider() -> DashboardState:
        return state

    if include_dashboard_routes:
        app.include_router(create_router(state_provider=state_provider))
    app.include_router(
        create_cc_router(
            state_provider=state_provider,
            executor=SimpleNamespace(),
            db_conn=object(),
        )
    )
    return TestClient(app)


def test_balances_includes_total_wallet_value_usd() -> None:
    state = DashboardState(
        portfolio=Portfolio(cash_usd=Decimal("100")),
        rotation_tree=_rotation_tree(total_portfolio_value_usd="112.50"),
    )

    response = _balances_client(state).get("/api/balances")

    assert response.status_code == 200
    assert response.json() == {
        "cash_usd": "100",
        "total_value_usd": "100",
        "total_wallet_value_usd": "112.50",
    }


def test_balances_total_value_usd_at_least_cash() -> None:
    state = DashboardState(
        portfolio=Portfolio(cash_usd=Decimal("256.37")),
        rotation_tree=_rotation_tree(
            total_portfolio_value_usd="256.37",
            node_specs=(("root-usd", "USD", "256.37"),),
        ),
    )

    response = _balances_client(state).get("/api/balances")

    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["cash_usd"]) == Decimal("256.37")
    assert Decimal(body["total_value_usd"]) >= Decimal("256.37")


def test_balances_wallet_matches_rotation_tree() -> None:
    state = DashboardState(
        portfolio=Portfolio(cash_usd=Decimal("100")),
        rotation_tree=_rotation_tree(total_portfolio_value_usd="112.50"),
    )
    client = _balances_client(state, include_dashboard_routes=True)

    balances_response = client.get("/api/balances")
    tree_response = client.get("/api/rotation-tree")

    assert balances_response.status_code == 200
    assert tree_response.status_code == 200
    assert Decimal(balances_response.json()["total_wallet_value_usd"]) == Decimal(
        tree_response.json()["total_portfolio_value_usd"]
    )
