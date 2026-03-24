from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from core.config import Settings, load_settings
from core.types import (
    BeliefDirection,
    BeliefSnapshot,
    BeliefSource,
    BotState,
    EventType,
    MarketRegime,
    Portfolio,
    Position,
    PositionSide,
)
from guardian import GuardianAction, GuardianActionType
from scheduler import (
    DashboardStateUpdate,
    ReconciliationDiscrepancy,
    Scheduler,
    SchedulerConfig,
    SchedulerState,
)
from trading.reconciler import (
    ForeignOrderClassification,
    KrakenOrder,
    KrakenState,
    ReconciliationAction,
    ReconciliationSeverity,
    SupabaseState,
)

NOW = datetime(2026, 3, 24, 12, 0, tzinfo=timezone.utc)


def _settings(**overrides: str) -> Settings:
    env = {
        "KRAKEN_API_KEY": "key",
        "KRAKEN_API_SECRET": "secret",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_KEY": "supabase-key",
    }
    env.update(overrides)
    return load_settings(env)


def _scheduler() -> Scheduler:
    return Scheduler(
        config=SchedulerConfig(
            cycle_interval_sec=30,
            reconcile_interval_sec=300,
            guardian_interval_sec=120,
        ),
        settings=_settings(),
    )


def _position(
    *,
    position_id: str,
    pair: str,
    side: PositionSide = PositionSide.LONG,
    quantity: str = "1",
    entry_price: str = "100",
    stop_price: str = "95",
    target_price: str = "110",
) -> Position:
    return Position(
        position_id=position_id,
        pair=pair,
        side=side,
        quantity=Decimal(quantity),
        entry_price=Decimal(entry_price),
        stop_price=Decimal(stop_price),
        target_price=Decimal(target_price),
    )


def _portfolio(*, positions: tuple[Position, ...] = ()) -> Portfolio:
    cash_usd = Decimal("1000")
    total_value = cash_usd + sum(
        (
            position.quantity * position.entry_price
            if position.side == PositionSide.LONG
            else -(position.quantity * position.entry_price)
            for position in positions
        ),
        start=Decimal("0"),
    )
    return Portfolio(
        cash_usd=cash_usd,
        positions=positions,
        total_value_usd=total_value,
    )


def test_run_cycle_returns_only_dashboard_update_when_no_work_is_due() -> None:
    scheduler = _scheduler()
    state = SchedulerState(
        bot_state=BotState(portfolio=_portfolio()),
        now=NOW,
        last_guardian_check_at=NOW,
        last_reconcile_at=NOW,
    )

    new_state, actions = scheduler.run_cycle(state)

    assert new_state.bot_state == state.bot_state
    assert new_state.cycle_count == 1
    assert new_state.last_cycle_at == NOW
    assert actions == (
        DashboardStateUpdate(
            bot_state=new_state.bot_state,
            cycle_count=1,
            emitted_at=NOW,
            next_cycle_due_at=NOW + timedelta(seconds=30),
        ),
    )


def test_run_cycle_emits_guardian_stop_actions_when_stop_is_hit() -> None:
    scheduler = _scheduler()
    state = SchedulerState(
        bot_state=BotState(
            portfolio=_portfolio(
                positions=(_position(position_id="btc-1", pair="BTC/USD"),),
            )
        ),
        current_prices={"BTC/USD": Decimal("94")},
        now=NOW,
        last_reconcile_at=NOW,
    )

    new_state, actions = scheduler.run_cycle(state)

    guardian_actions = [action for action in actions if isinstance(action, GuardianAction)]
    assert [action.action_type for action in guardian_actions] == [
        GuardianActionType.STOP_TRIGGERED,
        GuardianActionType.LIMIT_EXIT_ATTEMPT,
    ]
    assert new_state.last_guardian_check_at == NOW
    assert new_state.bot_state.last_event is EventType.STOP_TRIGGERED
    assert isinstance(actions[-1], DashboardStateUpdate)


def test_run_cycle_records_reconciliation_discrepancies_when_due() -> None:
    scheduler = _scheduler()
    state = SchedulerState(
        bot_state=BotState(portfolio=_portfolio()),
        kraken_state=KrakenState(
            open_orders=(
                KrakenOrder(
                    order_id="foreign-1",
                    pair="BTC/USD",
                    client_order_id="manual-order",
                    opened_at=NOW - timedelta(minutes=10),
                ),
            ),
        ),
        supabase_state=SupabaseState(),
        now=NOW,
        last_guardian_check_at=NOW,
    )

    new_state, actions = scheduler.run_cycle(state)

    discrepancy = next(
        action for action in actions if isinstance(action, ReconciliationDiscrepancy)
    )
    assert discrepancy.report.discrepancy_detected is True
    assert discrepancy.report.foreign_orders[0].classification is ForeignOrderClassification.NEW
    assert discrepancy.report.foreign_orders[0].severity is ReconciliationSeverity.LOW
    assert discrepancy.report.foreign_orders[0].recommended_action is ReconciliationAction.AUTO_FIX
    assert new_state.last_reconcile_at == NOW
    assert new_state.last_reconciliation_report == discrepancy.report
    assert new_state.bot_state.last_event is EventType.RECONCILIATION_RESULT
    assert isinstance(actions[-1], DashboardStateUpdate)


def test_run_cycle_consumes_pending_belief_signals_and_updates_bot_state() -> None:
    scheduler = _scheduler()
    prior_belief = BeliefSnapshot(
        pair="DOGE/USD",
        direction=BeliefDirection.BEARISH,
        confidence=0.31,
        regime=MarketRegime.RANGING,
        sources=(BeliefSource.CLAUDE,),
    )
    fresh_belief = BeliefSnapshot(
        pair="DOGE/USD",
        direction=BeliefDirection.BULLISH,
        confidence=0.82,
        regime=MarketRegime.TRENDING,
        sources=(BeliefSource.CODEX,),
    )
    state = SchedulerState(
        bot_state=BotState(portfolio=_portfolio(), beliefs=(prior_belief,)),
        pending_belief_signals=(fresh_belief,),
        now=NOW,
        last_guardian_check_at=NOW,
        last_reconcile_at=NOW,
    )

    new_state, actions = scheduler.run_cycle(state)

    assert new_state.pending_belief_signals == ()
    assert new_state.bot_state.beliefs == (fresh_belief,)
    assert new_state.bot_state.last_event is EventType.BELIEF_UPDATE
    assert actions == (
        DashboardStateUpdate(
            bot_state=new_state.bot_state,
            cycle_count=1,
            emitted_at=NOW,
            next_cycle_due_at=NOW + timedelta(seconds=30),
        ),
    )
