from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from core.types import Balance
from trading.reconciler import (
    FeeDrift,
    ForeignOrderClassification,
    GhostPosition,
    KrakenOrder,
    KrakenState,
    KrakenTrade,
    ReconciliationAction,
    ReconciliationReport,
    ReconciliationSeverity,
    SupabaseOrder,
    SupabasePosition,
    SupabaseState,
    UntrackedAsset,
    reconcile,
)

AS_OF = datetime(2026, 3, 24, 12, 0, 0)


def _order(
    order_id: str,
    *,
    pair: str,
    client_order_id: str | None,
    opened_minutes_ago: int,
) -> KrakenOrder:
    return KrakenOrder(
        order_id=order_id,
        pair=pair,
        client_order_id=client_order_id,
        opened_at=AS_OF - timedelta(minutes=opened_minutes_ago),
    )


def _trade(
    trade_id: str,
    *,
    pair: str,
    fee: str,
    order_id: str | None = None,
    client_order_id: str | None = None,
    position_id: str | None = None,
    filled_minutes_ago: int = 0,
) -> KrakenTrade:
    return KrakenTrade(
        trade_id=trade_id,
        pair=pair,
        order_id=order_id,
        client_order_id=client_order_id,
        position_id=position_id,
        fee=Decimal(fee),
        filled_at=AS_OF - timedelta(minutes=filled_minutes_ago),
    )


def _sb_order(
    order_id: str,
    *,
    pair: str,
    position_id: str | None = None,
    exchange_order_id: str | None = None,
    client_order_id: str | None = None,
    recorded_fee: str | None = None,
) -> SupabaseOrder:
    return SupabaseOrder(
        order_id=order_id,
        pair=pair,
        position_id=position_id,
        exchange_order_id=exchange_order_id,
        client_order_id=client_order_id,
        recorded_fee=None if recorded_fee is None else Decimal(recorded_fee),
    )


def test_reconcile_returns_clean_report_for_matching_state() -> None:
    report = reconcile(
        KrakenState(
            balances=(
                Balance(asset="BTC", available=Decimal("1")),
                Balance(asset="USD", available=Decimal("1000")),
            ),
            open_orders=(
                _order(
                    "kraken-1",
                    pair="BTC/USD",
                    client_order_id="kbv4-btcusd-000001",
                    opened_minutes_ago=5,
                ),
            ),
            trade_history=(
                _trade(
                    "trade-1",
                    pair="BTC/USD",
                    order_id="kraken-1",
                    client_order_id="kbv4-btcusd-000001",
                    fee="0.10",
                    filled_minutes_ago=1,
                ),
            ),
        ),
        SupabaseState(
            positions=(SupabasePosition(position_id="pos-1", pair="BTC/USD"),),
            orders=(
                _sb_order(
                    "sb-1",
                    pair="BTC/USD",
                    position_id="pos-1",
                    exchange_order_id="kraken-1",
                    client_order_id="kbv4-btcusd-000001",
                    recorded_fee="0.10",
                ),
            ),
        ),
        as_of=AS_OF,
    )

    assert report == ReconciliationReport()
    assert report.discrepancy_detected is False


def test_reconcile_flags_ghost_positions_without_live_exchange_support() -> None:
    report = reconcile(
        KrakenState(),
        SupabaseState(
            positions=(SupabasePosition(position_id="pos-ghost", pair="BTC/USD"),),
            orders=(
                _sb_order(
                    "sb-ghost",
                    pair="BTC/USD",
                    position_id="pos-ghost",
                    exchange_order_id="kraken-missing",
                    client_order_id="kbv4-btcusd-000999",
                ),
            ),
        ),
        as_of=AS_OF,
    )

    assert report.ghost_positions == (
        GhostPosition(
            position_id="pos-ghost",
            pair="BTC/USD",
            severity=ReconciliationSeverity.HIGH,
            recommended_action=ReconciliationAction.ALERT,
        ),
    )
    assert report.discrepancy_detected is True


def test_reconcile_classifies_foreign_orders_by_lifecycle() -> None:
    report = reconcile(
        KrakenState(
            open_orders=(
                _order("acked-1", pair="ETH/USD", client_order_id="legacy-acked", opened_minutes_ago=120),
                _order("new-1", pair="BTC/USD", client_order_id="manual-new", opened_minutes_ago=5),
                _order("stale-1", pair="DOGE/USD", client_order_id=None, opened_minutes_ago=90),
            ),
        ),
        SupabaseState(
            orders=(
                _sb_order(
                    "sb-acked",
                    pair="ETH/USD",
                    exchange_order_id="acked-1",
                    client_order_id="legacy-acked",
                ),
            ),
        ),
        stale_order_age=timedelta(minutes=30),
        as_of=AS_OF,
    )

    foreign_orders = {order.order_id: order for order in report.foreign_orders}

    assert foreign_orders["acked-1"].classification == ForeignOrderClassification.ACKED
    assert foreign_orders["acked-1"].recommended_action == ReconciliationAction.AUTO_FIX
    assert foreign_orders["new-1"].classification == ForeignOrderClassification.NEW
    assert foreign_orders["new-1"].recommended_step == "cancel_foreign_order"
    assert foreign_orders["stale-1"].classification == ForeignOrderClassification.STALE
    assert foreign_orders["stale-1"].severity == ReconciliationSeverity.HIGH
    assert foreign_orders["stale-1"].recommended_action == ReconciliationAction.ALERT


def test_reconcile_detects_fee_drift_against_supabase_fees() -> None:
    report = reconcile(
        KrakenState(
            trade_history=(
                _trade(
                    "trade-fee",
                    pair="BTC/USD",
                    order_id="kraken-fee-1",
                    client_order_id="kbv4-btcusd-000002",
                    fee="0.17",
                ),
            ),
        ),
        SupabaseState(
            orders=(
                _sb_order(
                    "sb-fee-1",
                    pair="BTC/USD",
                    exchange_order_id="kraken-fee-1",
                    client_order_id="kbv4-btcusd-000002",
                    recorded_fee="0.10",
                ),
            ),
        ),
        fee_drift_tolerance=Decimal("0.05"),
        high_fee_drift_tolerance=Decimal("0.10"),
        as_of=AS_OF,
    )

    assert report.fee_drift == (
        FeeDrift(
            order_id="sb-fee-1",
            pair="BTC/USD",
            kraken_fee=Decimal("0.17"),
            supabase_fee=Decimal("0.10"),
            delta=Decimal("0.07"),
            severity=ReconciliationSeverity.LOW,
            recommended_action=ReconciliationAction.AUTO_FIX,
            recommended_step="sync_fee_ledger",
        ),
    )


def test_reconcile_maps_low_and_high_severity_to_expected_actions() -> None:
    report = reconcile(
        KrakenState(
            balances=(Balance(asset="DOGE", available=Decimal("250")),),
            open_orders=(
                _order("stale-foreign", pair="LTC/USD", client_order_id="legacy-order", opened_minutes_ago=120),
            ),
            trade_history=(
                _trade(
                    "trade-high-fee",
                    pair="BTC/USD",
                    order_id="kraken-fee-2",
                    fee="0.40",
                ),
            ),
        ),
        SupabaseState(
            orders=(
                _sb_order(
                    "sb-fee-2",
                    pair="BTC/USD",
                    exchange_order_id="kraken-fee-2",
                    recorded_fee="0.10",
                ),
            ),
        ),
        stale_order_age=timedelta(minutes=30),
        fee_drift_tolerance=Decimal("0.05"),
        high_fee_drift_tolerance=Decimal("0.10"),
        as_of=AS_OF,
    )

    assert report.untracked_assets == (
        UntrackedAsset(
            asset="DOGE",
            available=Decimal("250"),
            held=Decimal("0"),
            severity=ReconciliationSeverity.LOW,
            recommended_action=ReconciliationAction.AUTO_FIX,
        ),
    )
    assert report.foreign_orders[0].severity == ReconciliationSeverity.HIGH
    assert report.foreign_orders[0].recommended_action == ReconciliationAction.ALERT
    assert report.fee_drift[0].severity == ReconciliationSeverity.HIGH
    assert report.fee_drift[0].recommended_action == ReconciliationAction.ALERT
