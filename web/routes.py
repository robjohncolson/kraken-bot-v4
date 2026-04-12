from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, field_validator

from core.types import (
    BeliefDirection,
    BeliefSource,
    GridPhase,
    MarketRegime,
    Pair,
    Portfolio,
    Position,
    ZERO_DECIMAL,
)
from trading.reconciler import ReconciliationReport


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    position: Position
    current_price: Decimal
    unrealized_pnl_usd: Decimal = ZERO_DECIMAL


@dataclass(frozen=True, slots=True)
class GridPhaseCount:
    phase: GridPhase
    active_slots: int


@dataclass(frozen=True, slots=True)
class GridCycleSnapshot:
    cycle_id: str
    realized_pnl_usd: Decimal = ZERO_DECIMAL
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GridStatusSnapshot:
    pair: Pair
    active_slots: int
    phase_distribution: tuple[GridPhaseCount, ...] = field(default_factory=tuple)
    cycle_history: tuple[GridCycleSnapshot, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class BeliefEntry:
    pair: Pair
    source: BeliefSource
    direction: BeliefDirection
    confidence: float
    regime: MarketRegime = MarketRegime.UNKNOWN
    updated_at: datetime | None = None
    filtered: bool = False  # True if below confidence gate (display-only, not used for trading)


@dataclass(frozen=True, slots=True)
class StrategyStatsSnapshot:
    trade_count: int = 0
    win_rate: float | None = None
    win_rate_ci_low: float | None = None
    win_rate_ci_high: float | None = None
    sharpe_ratio: float | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    checked_at: datetime | None = None
    report: ReconciliationReport | None = None


@dataclass(frozen=True, slots=True)
class RotationNodeSnapshot:
    node_id: str
    parent_node_id: str | None
    depth: int
    asset: str
    quantity_total: str  # Decimal as string
    quantity_free: str
    quantity_reserved: str
    status: str
    entry_pair: str | None = None
    from_asset: str | None = None
    order_side: str | None = None
    entry_price: str | None = None
    confidence: float = 0.0
    deadline_at: str | None = None
    opened_at: str | None = None
    window_hours: float | None = None
    fill_price: str | None = None
    exit_price: str | None = None
    closed_at: str | None = None
    exit_proceeds: str | None = None
    realized_pnl: str | None = None
    ta_direction: str | None = None


@dataclass(frozen=True, slots=True)
class RotationTreeSnapshot:
    nodes: tuple[RotationNodeSnapshot, ...] = field(default_factory=tuple)
    root_node_ids: tuple[str, ...] = field(default_factory=tuple)
    max_depth: int = 2
    last_planned_at: str | None = None
    total_deployed: str = "0"
    total_realized_pnl: str = "0"
    open_count: int = 0
    closed_count: int = 0
    rotation_tree_value_usd: str = "0"
    total_portfolio_value_usd: str = "0"


@dataclass(frozen=True, slots=True)
class DashboardState:
    portfolio: Portfolio = field(default_factory=Portfolio)
    positions: tuple[PositionSnapshot, ...] = field(default_factory=tuple)
    grids: tuple[GridStatusSnapshot, ...] = field(default_factory=tuple)
    beliefs: tuple[BeliefEntry, ...] = field(default_factory=tuple)
    stats: StrategyStatsSnapshot = field(default_factory=StrategyStatsSnapshot)
    reconciliation: ReconciliationSnapshot = field(default_factory=ReconciliationSnapshot)
    rotation_tree: RotationTreeSnapshot = field(default_factory=RotationTreeSnapshot)
    pending_orders: tuple = field(default_factory=tuple)


class DashboardStateProvider(Protocol):
    def __call__(self) -> DashboardState: ...


class OrderPayload(BaseModel):
    pair: str
    side: str
    order_type: str = "limit"
    quantity: str
    limit_price: str | None = None
    stop_price: str | None = None

    @field_validator("side")
    @classmethod
    def validate_side(cls, v: str) -> str:
        if v.lower() not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        return v.lower()

    @field_validator("order_type")
    @classmethod
    def validate_order_type(cls, v: str) -> str:
        if v.lower() not in ("market", "limit", "stop_loss"):
            raise ValueError("order_type must be 'market', 'limit', or 'stop_loss'")
        return v.lower()

    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, v: str) -> str:
        d = Decimal(v)
        if d <= 0 or not d.is_finite():
            raise ValueError("quantity must be a positive finite number")
        return v

    @field_validator("limit_price", "stop_price")
    @classmethod
    def validate_price(cls, v: str | None) -> str | None:
        if v is not None:
            d = Decimal(v)
            if d <= 0 or not d.is_finite():
                raise ValueError("price must be a positive finite number")
        return v


def create_router(*, state_provider: DashboardStateProvider) -> APIRouter:
    router = APIRouter(prefix="/api")

    def get_state() -> DashboardState:
        return state_provider()

    @router.get("/portfolio")
    def read_portfolio(state: DashboardState = Depends(get_state)) -> dict[str, Any]:
        return _encode_payload(state.portfolio)

    @router.get("/positions")
    def read_positions(state: DashboardState = Depends(get_state)) -> dict[str, Any]:
        return {"positions": _encode_payload(state.positions)}

    @router.get("/grid/{pair:path}")
    def read_grid(pair: str, state: DashboardState = Depends(get_state)) -> dict[str, Any]:
        for grid in state.grids:
            if grid.pair == pair:
                return _serialize_grid_status(grid)
        raise HTTPException(status_code=404, detail=f"Grid state not found for pair {pair!r}.")

    @router.get("/beliefs")
    def read_beliefs(state: DashboardState = Depends(get_state)) -> dict[str, Any]:
        return {"beliefs": _serialize_beliefs(state.beliefs)}

    @router.get("/stats")
    def read_stats(state: DashboardState = Depends(get_state)) -> dict[str, Any]:
        return _encode_payload(state.stats)

    @router.get("/reconciliation")
    def read_reconciliation(state: DashboardState = Depends(get_state)) -> dict[str, Any]:
        return _serialize_reconciliation(state.reconciliation)

    @router.get("/rotation-tree")
    def read_rotation_tree(state: DashboardState = Depends(get_state)) -> dict[str, Any]:
        return _encode_payload(state.rotation_tree)

    return router


def _serialize_grid_status(grid: GridStatusSnapshot) -> dict[str, Any]:
    return {
        "pair": grid.pair,
        "active_slots": grid.active_slots,
        "phase_distribution": {
            item.phase.value: item.active_slots for item in grid.phase_distribution
        },
        "cycle_history": _encode_payload(grid.cycle_history),
    }


def _serialize_beliefs(beliefs: tuple[BeliefEntry, ...]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for belief in beliefs:
        payload = _encode_payload(belief)
        pair = payload.pop("pair")
        source = payload.pop("source")
        grouped.setdefault(pair, {})[source] = payload
    return grouped


def _serialize_reconciliation(snapshot: ReconciliationSnapshot) -> dict[str, Any]:
    report = snapshot.report
    payload: dict[str, Any] = {
        "checked_at": _encode_payload(snapshot.checked_at),
        "discrepancy_detected": False if report is None else report.discrepancy_detected,
        "ghost_positions": [],
        "foreign_orders": [],
        "fee_drift": [],
        "untracked_assets": [],
    }
    report_payload = _encode_payload(report)
    if isinstance(report_payload, dict):
        payload.update(report_payload)
    return payload


def _encode_payload(payload: object) -> Any:
    return jsonable_encoder(payload)


# ---------------------------------------------------------------------------
# CC Command API — endpoints for Claude Code to read data and place orders
# ---------------------------------------------------------------------------


def create_cc_router(
    *,
    state_provider: DashboardStateProvider,
    executor: object,
    db_conn: object,
) -> APIRouter:
    """Create REST endpoints for CC (Claude Code) to act as the trading brain.

    All blocking calls (executor, SQLite, OHLCV HTTP) run in a thread pool
    via run_in_executor to avoid blocking the async event loop.
    CC-placed orders are reconciled by the bot's periodic reconciliation loop
    which checks Kraken's trade history every reconcile_interval_sec.
    """
    import asyncio
    import logging
    import sqlite3
    from functools import partial

    import pandas as pd

    from core.errors import ExchangeError, SafeModeBlockedError
    from core.types import OrderRequest, OrderSide, OrderType
    from exchange.ohlcv import OHLCVFetchError, fetch_ohlcv
    from persistence.sqlite import SqliteReader, SqliteWriter

    _log = logging.getLogger(__name__)
    router = APIRouter(prefix="/api")
    # Create a separate SQLite connection for the CC router thread pool
    # (the runtime's connection has check_same_thread=True by default)
    _cc_reader = None
    _cc_writer = None
    for candidate in (
        getattr(executor, "_writer", None),
        getattr(executor, "writer", None),
        db_conn,
    ):
        if hasattr(candidate, "upsert_order"):
            _cc_writer = candidate
            break
    if isinstance(db_conn, sqlite3.Connection):
        db_path = db_conn.execute("PRAGMA database_list").fetchone()[2]
        if db_path:
            _cc_conn = sqlite3.connect(db_path, check_same_thread=False)
            _cc_conn.row_factory = sqlite3.Row
            _cc_reader = SqliteReader(_cc_conn)
            _cc_writer = SqliteWriter(_cc_conn)
    reader = _cc_reader
    writer = _cc_writer

    def _validate_order_params(payload: OrderPayload) -> None:
        if payload.order_type == "limit" and not payload.limit_price:
            raise ValueError("limit_price is required for limit orders")
        if payload.order_type == "stop_loss" and not payload.stop_price:
            raise ValueError("stop_price is required for stop_loss orders")

    @router.post("/orders")
    async def place_order(payload: OrderPayload) -> dict[str, Any]:
        if not hasattr(executor, "execute_order"):
            raise HTTPException(status_code=503, detail="Executor not available")
        _validate_order_params(payload)
        order = OrderRequest(
            pair=payload.pair,
            side=OrderSide(payload.side),
            order_type=OrderType(payload.order_type),
            quantity=Decimal(payload.quantity),
            limit_price=Decimal(payload.limit_price) if payload.limit_price else None,
            stop_price=Decimal(payload.stop_price) if payload.stop_price else None,
        )
        try:
            loop = asyncio.get_event_loop()
            txid = await loop.run_in_executor(None, executor.execute_order, order)
            _log.info("CC placed order %s on %s", txid, payload.pair)
            response: dict[str, Any] = {
                "txid": txid,
                "status": "placed",
                "pair": payload.pair,
            }
            if writer is not None:
                try:
                    await loop.run_in_executor(
                        None,
                        partial(
                            writer.upsert_order,
                            order_id=txid,
                            pair=payload.pair,
                            client_order_id=f"kbv4-cc-{txid}",
                            kind="cc_api",
                            side=order.side.value,
                            base_qty=order.quantity,
                            filled_qty=ZERO_DECIMAL,
                            quote_qty=ZERO_DECIMAL,
                            limit_price=order.limit_price,
                            exchange_order_id=txid,
                            rotation_node_id=None,
                        ),
                    )
                except Exception as exc:
                    response["warning"] = (
                        "Order placed on Kraken but failed to persist to SQLite."
                    )
                    _log.warning(
                        "CC order %s on %s placed but persistence failed: %s",
                        txid,
                        payload.pair,
                        exc,
                    )
            return response
        except SafeModeBlockedError as exc:
            raise HTTPException(status_code=403, detail="Safe mode is enabled") from exc
        except ExchangeError as exc:
            _log.warning("CC order exchange error on %s: %s", payload.pair, exc)
            raise HTTPException(status_code=502, detail=f"Exchange error: {exc}") from exc
        except Exception as exc:
            _log.warning("CC order failed on %s: %s", payload.pair, exc)
            raise HTTPException(status_code=400, detail=f"Order rejected: {exc}") from exc

    @router.delete("/orders/{order_id}")
    async def cancel_order(order_id: str) -> dict[str, Any]:
        if not hasattr(executor, "execute_cancel"):
            raise HTTPException(status_code=503, detail="Executor not available")
        try:
            loop = asyncio.get_event_loop()
            count = await loop.run_in_executor(None, executor.execute_cancel, order_id)
            return {"status": "cancelled", "count": count, "order_id": order_id}
        except SafeModeBlockedError as exc:
            raise HTTPException(status_code=403, detail="Safe mode is enabled") from exc
        except ExchangeError as exc:
            raise HTTPException(status_code=502, detail="Exchange error") from exc
        except Exception as exc:
            _log.warning("CC cancel failed: %s", exc)
            raise HTTPException(status_code=400, detail="Cancel rejected") from exc

    @router.get("/trade-outcomes")
    async def get_trade_outcomes(
        lookback_days: int = 30,
    ) -> dict[str, Any]:
        if reader is None:
            raise HTTPException(status_code=503, detail="Database not available")
        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(
            None, partial(reader.fetch_trade_outcomes, lookback_days=lookback_days),
        )
        outcomes = [{col: row[i] for i, col in enumerate(row.keys())} for row in rows]
        return {"outcomes": outcomes, "count": len(outcomes)}

    @router.get("/ohlcv/{pair:path}")
    async def get_ohlcv(
        pair: str,
        interval: int = 60,
        count: int = 50,
    ) -> dict[str, Any]:
        try:
            loop = asyncio.get_event_loop()
            bars = await loop.run_in_executor(
                None, partial(fetch_ohlcv, pair, interval=interval, count=count),
            )
            records = [
                {
                    "open": str(row["open"]),
                    "high": str(row["high"]),
                    "low": str(row["low"]),
                    "close": str(row["close"]),
                    "volume": str(row["volume"]),
                }
                for _, row in bars.iterrows()
            ]
            return {"pair": pair, "interval": interval, "bars": records}
        except OHLCVFetchError as exc:
            raise HTTPException(status_code=404, detail="OHLCV data unavailable") from exc

    @router.get("/kronos/{pair:path}")
    async def get_kronos_prediction(
        pair: str,
        interval: int = 60,
        count: int = 400,
        pred_len: int = 24,
    ) -> dict[str, Any]:
        """Kronos candle prediction — returns predicted OHLCV for next pred_len bars."""

        def _run_kronos() -> dict[str, Any]:
            import sys

            kronos_path = "C:/Users/rober/Downloads/Projects/kronos"
            if kronos_path not in sys.path:
                sys.path.insert(0, kronos_path)

            bars = fetch_ohlcv(pair, interval=interval, count=count)
            if len(bars) < 40:
                return {"error": "Insufficient bars", "count": len(bars)}

            ohlcv_df = bars[["open", "high", "low", "close", "volume"]].astype(float).reset_index(drop=True)
            x_ts = pd.Series(pd.date_range(
                end=pd.Timestamp.now(), periods=len(ohlcv_df), freq=f"{interval}min",
            ))
            y_ts = pd.Series(pd.date_range(
                start=x_ts.iloc[-1] + pd.Timedelta(minutes=interval),
                periods=pred_len, freq=f"{interval}min",
            ))

            from model import Kronos as KronosModel
            from model import KronosPredictor, KronosTokenizer

            import torch

            _cache = getattr(_run_kronos, "_cache", None)
            if _cache is None:
                tok = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
                mdl = KronosModel.from_pretrained("NeoQuasar/Kronos-mini")
                if torch.xpu.is_available():
                    tok = tok.to("xpu")
                    mdl = mdl.to("xpu")
                pred = KronosPredictor(mdl, tok, max_context=2048)
                _run_kronos._cache = pred
                _cache = pred

            pred_df = _cache.predict(
                df=ohlcv_df, x_timestamp=x_ts, y_timestamp=y_ts,
                pred_len=pred_len, T=1.0, top_p=0.9, sample_count=1, verbose=False,
            )
            last_close = float(ohlcv_df["close"].iloc[-1])
            pred_close = float(pred_df["close"].iloc[-1])
            pct_change = (pred_close - last_close) / last_close * 100
            direction = "bullish" if pct_change > 0.5 else "bearish" if pct_change < -0.5 else "neutral"
            pred_high = float(pred_df["high"].max())
            pred_low = float(pred_df["low"].min())
            volatility_pct = (pred_high - pred_low) / last_close * 100

            candles = [
                {
                    "timestamp": str(pred_df.index[i]),
                    "open": round(float(pred_df["open"].iloc[i]), 4),
                    "high": round(float(pred_df["high"].iloc[i]), 4),
                    "low": round(float(pred_df["low"].iloc[i]), 4),
                    "close": round(float(pred_df["close"].iloc[i]), 4),
                }
                for i in range(len(pred_df))
            ]
            return {
                "pair": pair,
                "interval": interval,
                "pred_len": pred_len,
                "current_close": round(last_close, 4),
                "predicted_close": round(pred_close, 4),
                "pct_change": round(pct_change, 2),
                "direction": direction,
                "predicted_range": {"high": round(pred_high, 4), "low": round(pred_low, 4)},
                "volatility_pct": round(volatility_pct, 2),
                "candles": candles,
            }

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _run_kronos)
            if "error" in result:
                raise HTTPException(status_code=400, detail=result["error"])
            return result
        except HTTPException:
            raise
        except Exception as exc:
            _log.warning("Kronos prediction failed for %s: %s", pair, exc)
            raise HTTPException(status_code=500, detail="Prediction failed") from exc

    @router.get("/timesfm/{pair:path}")
    async def get_timesfm_prediction(
        pair: str,
    ) -> dict[str, Any]:
        """TimesFM close-price forecast — direction + confidence."""
        from beliefs.timesfm_source import TimesFMSource

        _cache: list = getattr(
            get_timesfm_prediction, "_cache", []
        )
        if not _cache:
            _cache.append(TimesFMSource())
            get_timesfm_prediction._cache = _cache  # type: ignore[attr-defined]
        source = _cache[0]

        def _run() -> dict[str, Any]:
            bars = fetch_ohlcv(pair, interval=60, count=520)
            if len(bars) < source.min_bars:
                return {
                    "error": "Insufficient bars",
                    "count": len(bars),
                }
            snap = source.analyze(pair, bars)
            return {
                "pair": pair,
                "direction": snap.direction.value,
                "confidence": round(snap.confidence, 3),
                "regime": snap.regime.value,
            }

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _run)
            if "error" in result:
                raise HTTPException(
                    status_code=400, detail=result["error"],
                )
            return result
        except HTTPException:
            raise
        except Exception as exc:
            _log.warning(
                "TimesFM prediction failed for %s: %s", pair, exc,
            )
            raise HTTPException(
                status_code=500, detail="Prediction failed",
            ) from exc

    @router.get("/regime/{pair:path}")
    async def get_regime(
        pair: str,
        interval: int = 60,
        count: int = 300,
    ) -> dict[str, Any]:
        """HMM regime detection — returns P(trending), P(ranging), P(volatile)."""
        from trading.regime_detector import detect_regime

        def _run_regime() -> dict[str, Any]:
            bars = fetch_ohlcv(pair, interval=interval, count=count)
            result = detect_regime(bars)
            return {
                "pair": pair,
                "interval": interval,
                "bars_used": len(bars),
                "regime": result.regime,
                "confidence": result.confidence,
                "probabilities": result.probabilities,
                "trade_gate": result.trade_gate,
            }

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _run_regime)
        except OHLCVFetchError as exc:
            raise HTTPException(status_code=404, detail="OHLCV data unavailable") from exc
        except Exception as exc:
            _log.warning("Regime detection failed for %s: %s", pair, exc)
            raise HTTPException(status_code=500, detail="Regime detection failed") from exc

    # === CC Memory endpoints ===

    @router.get("/memory")
    async def query_memory(
        category: str | None = None,
        pair: str | None = None,
        hours: int = 24,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query CC's temporal memory."""
        from persistence.cc_memory import CCMemory

        if reader is None:
            raise HTTPException(status_code=503, detail="Database not available")

        def _query() -> dict[str, Any]:
            mem = CCMemory(reader._conn)
            results = mem.query(category=category, pair=pair, hours=hours, limit=limit)
            return {"memories": results, "count": len(results)}

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query)

    @router.post("/memory")
    async def write_memory(payload: dict[str, Any]) -> dict[str, Any]:
        """Write to CC's temporal memory."""
        from persistence.cc_memory import CCMemory

        if reader is None:
            raise HTTPException(status_code=503, detail="Database not available")
        category = payload.get("category")
        if not category:
            raise HTTPException(status_code=400, detail="category is required")

        def _write() -> dict[str, Any]:
            mem = CCMemory(reader._conn)
            content = payload.get("content", {})
            pair = payload.get("pair")
            importance = payload.get("importance", 0.5)
            mem_id = mem._write(category, content, pair=pair, importance=importance)
            return {"id": mem_id, "status": "stored"}

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _write)

    def get_state_for_cc() -> DashboardState:
        return state_provider()

    @router.get("/balances")
    async def get_balances(
        state: DashboardState = Depends(get_state_for_cc),
    ) -> dict[str, Any]:
        portfolio = state.portfolio
        return {
            "cash_usd": str(portfolio.cash_usd) if portfolio else "0",
            "total_value_usd": str(portfolio.total_value_usd) if portfolio else "0",
        }

    @router.get("/exchange-balances")
    async def get_exchange_balances() -> dict[str, Any]:
        """Fetch live balances directly from Kraken — ground truth."""
        if not hasattr(executor, "fetch_balances"):
            raise HTTPException(status_code=503, detail="Executor not available")
        try:
            loop = asyncio.get_event_loop()
            balances = await loop.run_in_executor(None, executor.fetch_balances)
            return {
                "balances": [
                    {"asset": b.asset, "available": str(b.available), "held": str(b.held)}
                    for b in balances if (b.available + b.held) > 0
                ],
                "count": sum(1 for b in balances if (b.available + b.held) > 0),
            }
        except Exception as exc:
            _log.warning("exchange-balances failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"Exchange error: {exc}") from exc

    @router.get("/open-orders")
    async def get_open_orders() -> dict[str, Any]:
        """Fetch live open orders directly from Kraken."""
        if not hasattr(executor, "fetch_open_orders"):
            raise HTTPException(status_code=503, detail="Executor not available")
        try:
            loop = asyncio.get_event_loop()
            orders = await loop.run_in_executor(None, executor.fetch_open_orders)
            return {
                "orders": [
                    {
                        "txid": order.order_id,
                        "pair": order.pair,
                        "side": order.side,
                        "volume": str(order.volume),
                        "volume_executed": str(order.volume_executed),
                        "price": str(order.price),
                        "status": order.status,
                        "opentm": (
                            order.opentm
                            if order.opentm is not None
                            else (
                                order.opened_at.timestamp()
                                if order.opened_at is not None
                                else None
                            )
                        ),
                    }
                    for order in orders
                ],
                "count": len(orders),
            }
        except Exception as exc:
            _log.warning("open-orders failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"Exchange error: {exc}") from exc

    return router


__all__ = [
    "BeliefEntry",
    "DashboardState",
    "DashboardStateProvider",
    "GridCycleSnapshot",
    "GridPhaseCount",
    "GridStatusSnapshot",
    "PositionSnapshot",
    "ReconciliationSnapshot",
    "StrategyStatsSnapshot",
    "create_cc_router",
    "create_router",
]
