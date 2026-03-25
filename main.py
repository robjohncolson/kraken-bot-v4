"""kraken-bot-v4 entry point.

Startup sequence:
  1. Load .env and validate config (fail fast on missing vars)
  2. Ensure local state dir + open SQLite + ensure schema
  3. Health-check Kraken connectivity
  4. Fetch Kraken state (balances, open orders, trade history)
  5. Fetch recorded state from SQLite
  6. Reconcile Kraken state vs recorded state
  7. If STARTUP_RECONCILE_ONLY=true, log report and exit
  8. Start main scheduler loop (stub until scheduler wired)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import sqlite3

from core.config import Settings, load_settings
from core.errors import ConfigError, ExchangeError
from exchange.client import KrakenClient
from exchange.executor import KrakenExecutor
from exchange.models import KrakenState
from exchange.order_gate import OrderGate
from exchange.transport import HttpKrakenTransport
from persistence.sqlite import (
    SqlitePersistenceError,
    SqliteReader,
    ensure_schema,
    open_database,
)
from beliefs.autoresearch_handler import autoresearch_belief_handler
from runtime_loop import SchedulerRuntime, build_initial_scheduler_state
from trading.reconciler import ReconciliationReport, RecordedState, reconcile

logger = logging.getLogger("kraken-bot-v4")

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _load_dotenv() -> None:
    """Load .env file if present. No hard dependency on python-dotenv."""
    env_path = Path(".env")
    if not env_path.exists():
        logger.info(".env file not found, reading from environment only")
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import-untyped]

        load_dotenv(env_path, override=False)
        logger.info("Loaded .env file")
    except ImportError:
        logger.warning(
            "python-dotenv not installed; .env file exists but cannot be loaded. "
            "Install with: pip install python-dotenv"
        )


def _print_safe_mode_banner(settings: Settings) -> None:
    flags = []
    if settings.read_only_exchange:
        flags.append("READ_ONLY_EXCHANGE")
    if settings.disable_order_mutations:
        flags.append("DISABLE_ORDER_MUTATIONS")
    if settings.startup_reconcile_only:
        flags.append("STARTUP_RECONCILE_ONLY")

    if flags:
        logger.info("Safe mode active: %s", ", ".join(flags))
    else:
        logger.warning("ALL SAFE MODE FLAGS ARE OFF — live trading enabled")

    if settings.allowed_pairs:
        logger.info("Pair whitelist: %s", ", ".join(sorted(settings.allowed_pairs)))
    else:
        logger.info("Pair whitelist: NONE (no filtering — all pairs allowed)")


def _ensure_local_state_dir(settings: Settings) -> None:
    settings.local_state_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Local state dir: %s", settings.local_state_dir.resolve())


def _startup_healthcheck(settings: Settings) -> bool:
    """Validate that external services are reachable. Returns True if all OK."""
    ok = True

    # Kraken public API check (no auth needed)
    try:
        from urllib.request import urlopen

        resp = urlopen("https://api.kraken.com/0/public/SystemStatus", timeout=10)  # noqa: S310
        data = resp.read()
        import json

        status = json.loads(data).get("result", {}).get("status", "unknown")
        logger.info("Kraken API status: %s", status)
        if status != "online":
            logger.warning("Kraken API is not 'online' — status: %s", status)
    except (OSError, ValueError, KeyError) as exc:
        logger.error("Kraken API health check failed: %s", exc)
        ok = False

    # Telegram (optional)
    if settings.telegram_bot_token and settings.telegram_chat_id:
        logger.info("Telegram alerts: configured")
    else:
        logger.info("Telegram alerts: not configured (optional)")

    return ok


def _build_executor(settings: Settings) -> KrakenExecutor:
    """Construct the Kraken executor from settings."""
    client = KrakenClient(
        api_key=settings.kraken_api_key,
        api_secret=settings.kraken_api_secret,
        tier=settings.kraken_tier,
    )
    transport = HttpKrakenTransport()
    order_gate = OrderGate(client=client, allowed_pairs=settings.allowed_pairs)
    return KrakenExecutor(
        client=client,
        transport=transport,
        order_gate=order_gate,
        read_only_exchange=settings.read_only_exchange,
        disable_order_mutations=settings.disable_order_mutations,
    )


def _fetch_kraken_state(executor: KrakenExecutor) -> KrakenState | None:
    """Fetch live Kraken state. Returns None on failure."""
    logger.info("Fetching Kraken state (balances, open orders, trade history)...")
    try:
        state = executor.fetch_kraken_state()
    except ExchangeError as exc:
        logger.error("Failed to fetch Kraken state: %s", exc)
        return None

    for balance in state.balances:
        logger.info("  Balance: %s = %s", balance.asset, balance.available)
    logger.info("  Open orders: %d", len(state.open_orders))
    for order in state.open_orders:
        logger.info("    %s %s cl_ord=%s", order.order_id, order.pair, order.client_order_id)
    logger.info("  Recent trades: %d", len(state.trade_history))
    return state


def _open_sqlite(settings: Settings) -> sqlite3.Connection | None:
    """Open SQLite database and ensure schema. Returns None on failure."""
    try:
        conn = open_database(settings.sqlite_path)
        ensure_schema(conn)
        return conn
    except SqlitePersistenceError as exc:
        logger.error("SQLite startup failed: %s", exc)
        return None


def _fetch_recorded_state(conn: sqlite3.Connection) -> RecordedState | None:
    """Fetch recorded state from SQLite. Returns None on failure."""
    logger.info("Fetching recorded state from SQLite...")
    try:
        reader = SqliteReader(conn)
        state = reader.fetch_recorded_state()
    except SqlitePersistenceError as exc:
        logger.error("Failed to fetch recorded state: %s", exc)
        return None

    logger.info("  Open positions: %d", len(state.positions))
    logger.info("  Orders: %d", len(state.orders))
    return state


def _run_reconciliation(
    kraken_state: KrakenState, recorded_state: RecordedState
) -> ReconciliationReport:
    """Run startup reconciliation and log results."""
    logger.info("Running startup reconciliation...")
    report = reconcile(kraken_state, recorded_state)
    if report.discrepancy_detected:
        logger.warning("Reconciliation found discrepancies:")
        if report.ghost_positions:
            logger.warning("  Ghost positions: %d", len(report.ghost_positions))
        if report.foreign_orders:
            logger.warning("  Foreign orders: %d", len(report.foreign_orders))
        if report.fee_drift:
            logger.warning("  Fee drift: %d", len(report.fee_drift))
        if report.untracked_assets:
            logger.warning("  Untracked assets: %d", len(report.untracked_assets))
    else:
        logger.info("Reconciliation: clean — no discrepancies")
    return report


def _run_main_loop(
    settings: Settings,
    *,
    executor: KrakenExecutor,
    conn: sqlite3.Connection,
    kraken_state: KrakenState,
    recorded_state: RecordedState,
    report: ReconciliationReport,
) -> None:
    """Run the live scheduler runtime."""
    logger.info("Entering main scheduler loop...")

    if settings.read_only_exchange:
        logger.info("Exchange is READ-ONLY — no orders will be placed or cancelled")
    if settings.disable_order_mutations:
        logger.info("Order mutations are DISABLED — AddOrder/CancelOrder blocked")

    runtime = SchedulerRuntime(
        settings=settings,
        executor=executor,
        conn=conn,
        initial_state=build_initial_scheduler_state(
            kraken_state=kraken_state,
            recorded_state=recorded_state,
            report=report,
            now=datetime.now(timezone.utc),
        ),
        belief_refresh_handler=autoresearch_belief_handler,
    )
    try:
        asyncio.run(runtime.run_forever())
    except KeyboardInterrupt:
        logger.info("Received Ctrl+C — shutting down runtime loop")


def main() -> int:
    _setup_logging()
    logger.info("kraken-bot-v4 starting")

    # Step 1: Load config
    _load_dotenv()
    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error("Config validation failed: %s", exc)
        logger.error("Copy .env.example to .env and fill in required values")
        return 1

    _print_safe_mode_banner(settings)
    _ensure_local_state_dir(settings)

    # Step 2: Open SQLite
    conn = _open_sqlite(settings)
    if conn is None:
        return 1

    # Step 3: Health check Kraken
    healthy = _startup_healthcheck(settings)
    if not healthy:
        logger.error("Kraken health check failed — aborting")
        return 1

    # Step 4: Fetch Kraken state
    executor = _build_executor(settings)
    kraken_state = _fetch_kraken_state(executor)
    if kraken_state is None:
        logger.error("Could not fetch Kraken state — aborting")
        return 1

    # Step 5: Fetch recorded state from SQLite
    recorded_state = _fetch_recorded_state(conn)
    if recorded_state is None:
        logger.error("Could not fetch recorded state — aborting")
        return 1

    # Step 6: Reconcile
    report = _run_reconciliation(kraken_state, recorded_state)

    # Step 7: If reconcile-only mode, stop here
    if settings.startup_reconcile_only:
        logger.info("STARTUP_RECONCILE_ONLY=true — exiting after reconciliation")
        conn.close()
        return 0

    # Step 8: Main loop
    _run_main_loop(
        settings,
        executor=executor,
        conn=conn,
        kraken_state=kraken_state,
        recorded_state=recorded_state,
        report=report,
    )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
