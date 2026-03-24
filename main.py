"""kraken-bot-v4 entry point.

Startup sequence (SPEC.md section 8):
  1. Load .env and validate config (fail fast on missing vars)
  2. Health-check Kraken + Supabase connectivity
  3. Run startup reconciliation (stub until exchange client is wired)
  4. If STARTUP_RECONCILE_ONLY=true, print report and exit
  5. Start main scheduler loop (stub until exchange client is wired)
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from core.config import Settings, load_settings
from core.errors import ConfigError

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

        load_dotenv(env_path, override=True)
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

    # Supabase connectivity check
    try:
        from urllib.request import Request, urlopen

        req = Request(
            f"{settings.supabase_url.rstrip('/')}/rest/v1/",
            headers={
                "apikey": settings.supabase_key,
                "Authorization": f"Bearer {settings.supabase_key}",
            },
        )
        urlopen(req, timeout=10)  # noqa: S310
        logger.info("Supabase connection: OK (%s)", settings.supabase_url)
    except (OSError, ValueError) as exc:
        logger.error("Supabase health check failed: %s", exc)
        ok = False

    # Telegram (optional)
    if settings.telegram_bot_token and settings.telegram_chat_id:
        logger.info("Telegram alerts: configured")
    else:
        logger.info("Telegram alerts: not configured (optional)")

    return ok


def _run_reconciliation(settings: Settings) -> None:
    """Run startup reconciliation. Currently logs placeholder until exchange client is wired."""
    logger.info("Running startup reconciliation...")
    # TODO(task-2): Wire real Kraken client and Supabase client here
    # reconciler needs KrakenState (from exchange) and SupabaseState (from persistence)
    logger.info(
        "Reconciliation: STUB — exchange client not yet wired (task 2). "
        "Skipping real reconciliation."
    )


def _run_main_loop(settings: Settings) -> None:
    """Run the main scheduler loop. Currently a stub that logs heartbeats."""
    logger.info("Entering main scheduler loop...")

    if settings.read_only_exchange:
        logger.info("Exchange is READ-ONLY — no orders will be placed or cancelled")
    if settings.disable_order_mutations:
        logger.info("Order mutations are DISABLED — AddOrder/CancelOrder blocked")

    # TODO(task-2): Wire real Scheduler with live exchange + persistence
    cycle = 0
    try:
        while True:
            cycle += 1
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            logger.info("Heartbeat cycle=%d at=%s (stub — no real work yet)", cycle, now)
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Received Ctrl+C — shutting down after cycle %d", cycle)


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

    # Step 2-3: Health check external services
    healthy = _startup_healthcheck(settings)
    if not healthy:
        logger.error("Startup health check failed — aborting")
        return 1

    # Step 4: Reconcile
    _run_reconciliation(settings)

    # Step 5: If reconcile-only mode, stop here
    if settings.startup_reconcile_only:
        logger.info("STARTUP_RECONCILE_ONLY=true — exiting after reconciliation")
        return 0

    # Step 6: Main loop
    _run_main_loop(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
