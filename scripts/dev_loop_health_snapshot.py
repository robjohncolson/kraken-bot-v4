import json
import sqlite3
import sys
import urllib.request


SNAPSHOT_FIELDS = (
    "total_trades_24h",
    "total_trades_7d",
    "total_trades_prior_7d",
    "net_pnl_24h",
    "net_pnl_7d",
    "net_pnl_prior_7d",
    "win_rate_7d",
    "win_rate_prior_7d",
    "recon_errors_24h",
    "permission_blocked_pairs",
    "open_positions",
    "current_cash_usd",
    "current_total_value_usd",
)


def log_error(section: str, exc: Exception) -> None:
    print(f"[dev_loop_health_snapshot:{section}] {exc}", file=sys.stderr)


def build_empty_snapshot() -> dict[str, object | None]:
    return {field: None for field in SNAPSHOT_FIELDS}


def to_float(value: object | None) -> float | None:
    if value is None:
        return None
    return float(value)


def truthy_anomaly_sql(column_name: str) -> str:
    normalized = f"LOWER(TRIM(CAST({column_name} AS TEXT)))"
    return (
        f"{column_name} IS NOT NULL "
        f"AND TRIM(CAST({column_name} AS TEXT)) <> '' "
        f"AND {normalized} NOT IN ('0', 'false', 'no', 'off')"
    )


def trade_window_where(column_name: str, start_modifier: str, end_modifier: str | None = None) -> str:
    clauses = [
        f"{column_name} IS NOT NULL",
        f"datetime({column_name}) > datetime('now', '{start_modifier}')",
    ]
    if end_modifier is not None:
        clauses.append(f"datetime({column_name}) <= datetime('now', '{end_modifier}')")
    return " AND ".join(clauses)


def get_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def query_trade_window(
    conn: sqlite3.Connection,
    start_modifier: str,
    end_modifier: str | None = None,
    *,
    anomaly_column_present: bool,
) -> tuple[int, float]:
    pnl_expr = "CAST(net_pnl AS REAL)"
    if anomaly_column_present:
        pnl_expr = (
            f"CASE WHEN {truthy_anomaly_sql('anomaly_flag')} "
            f"THEN 0.0 ELSE CAST(net_pnl AS REAL) END"
        )
    sql = (
        "SELECT COUNT(*) AS trade_count, "
        f"COALESCE(SUM({pnl_expr}), 0.0) AS net_pnl "
        "FROM trade_outcomes "
        f"WHERE {trade_window_where('closed_at', start_modifier, end_modifier)}"
    )
    row = conn.execute(sql).fetchone()
    if row is None:
        return 0, 0.0
    return int(row[0]), float(row[1] or 0.0)


def query_win_rate(
    conn: sqlite3.Connection,
    start_modifier: str,
    end_modifier: str | None = None,
) -> float | None:
    sql = (
        "SELECT COUNT(*) AS total_count, "
        "SUM(CASE WHEN CAST(net_pnl AS REAL) > 0 THEN 1 ELSE 0 END) AS win_count "
        "FROM trade_outcomes "
        f"WHERE {trade_window_where('closed_at', start_modifier, end_modifier)}"
    )
    row = conn.execute(sql).fetchone()
    if row is None:
        return None
    total_count = int(row[0] or 0)
    win_count = int(row[1] or 0)
    if total_count == 0:
        return None
    return float(win_count) / float(total_count)


def query_recon_errors_24h(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM cc_memory
        WHERE category = 'reconciliation_anomaly'
          AND datetime(timestamp) > datetime('now', '-24 hours')
        """
    ).fetchone()
    return int(row[0] or 0) if row is not None else 0


def query_permission_blocked_pairs(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT CASE
            WHEN pair IS NULL OR TRIM(pair) = '' THEN NULL
            ELSE pair
        END)
        FROM cc_memory
        WHERE category = 'permission_blocked'
        """
    ).fetchone()
    return int(row[0] or 0) if row is not None else 0


def query_open_positions(conn: sqlite3.Connection) -> int:
    columns = get_table_columns(conn, "positions")
    if not columns:
        raise RuntimeError("positions table is missing or unreadable")
    if "status" in columns:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM positions
            WHERE status IS NULL
               OR TRIM(CAST(status AS TEXT)) = ''
               OR LOWER(TRIM(CAST(status AS TEXT))) = 'open'
            """
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM positions
            WHERE closed_at IS NULL
            """
        ).fetchone()
    return int(row[0] or 0) if row is not None else 0


def fetch_balances(balances_url: str) -> tuple[float | None, float | None]:
    request = urllib.request.Request(
        balances_url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset)
    data = json.loads(payload)
    return to_float(data.get("cash_usd")), to_float(data.get("total_value_usd"))


def populate_trade_metrics(snapshot: dict[str, object | None], conn: sqlite3.Connection) -> None:
    trade_columns = get_table_columns(conn, "trade_outcomes")
    anomaly_column_present = "anomaly_flag" in trade_columns

    try:
        total_trades_24h, net_pnl_24h = query_trade_window(
            conn,
            "-24 hours",
            anomaly_column_present=anomaly_column_present,
        )
        snapshot["total_trades_24h"] = total_trades_24h
        snapshot["net_pnl_24h"] = net_pnl_24h
    except Exception as exc:
        log_error("trade_window_24h", exc)

    try:
        total_trades_7d, net_pnl_7d = query_trade_window(
            conn,
            "-7 days",
            anomaly_column_present=anomaly_column_present,
        )
        snapshot["total_trades_7d"] = total_trades_7d
        snapshot["net_pnl_7d"] = net_pnl_7d
    except Exception as exc:
        log_error("trade_window_7d", exc)

    try:
        total_trades_prior_7d, net_pnl_prior_7d = query_trade_window(
            conn,
            "-14 days",
            "-7 days",
            anomaly_column_present=anomaly_column_present,
        )
        snapshot["total_trades_prior_7d"] = total_trades_prior_7d
        snapshot["net_pnl_prior_7d"] = net_pnl_prior_7d
    except Exception as exc:
        log_error("trade_window_prior_7d", exc)

    try:
        snapshot["win_rate_7d"] = query_win_rate(conn, "-7 days")
    except Exception as exc:
        log_error("win_rate_7d", exc)

    try:
        snapshot["win_rate_prior_7d"] = query_win_rate(conn, "-14 days", "-7 days")
    except Exception as exc:
        log_error("win_rate_prior_7d", exc)


def populate_memory_metrics(snapshot: dict[str, object | None], conn: sqlite3.Connection) -> None:
    try:
        snapshot["recon_errors_24h"] = query_recon_errors_24h(conn)
    except Exception as exc:
        log_error("recon_errors_24h", exc)

    try:
        snapshot["permission_blocked_pairs"] = query_permission_blocked_pairs(conn)
    except Exception as exc:
        log_error("permission_blocked_pairs", exc)


def populate_position_metrics(snapshot: dict[str, object | None], conn: sqlite3.Connection) -> None:
    try:
        snapshot["open_positions"] = query_open_positions(conn)
    except Exception as exc:
        log_error("open_positions", exc)


def populate_balance_metrics(snapshot: dict[str, object | None], balances_url: str) -> None:
    try:
        current_cash_usd, current_total_value_usd = fetch_balances(balances_url)
        snapshot["current_cash_usd"] = current_cash_usd
        snapshot["current_total_value_usd"] = current_total_value_usd
    except Exception as exc:
        log_error("balances", exc)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: dev_loop_health_snapshot.py <db_path> <balances_url>", file=sys.stderr)
        return 1

    db_path = sys.argv[1]
    balances_url = sys.argv[2]
    snapshot = build_empty_snapshot()

    conn: sqlite3.Connection | None = None
    try:
        try:
            conn = sqlite3.connect(db_path)
            populate_trade_metrics(snapshot, conn)
            populate_memory_metrics(snapshot, conn)
            populate_position_metrics(snapshot, conn)
        except Exception as exc:
            log_error("sqlite_connect", exc)
        finally:
            if conn is not None:
                conn.close()

        populate_balance_metrics(snapshot, balances_url)
        print(json.dumps(snapshot, separators=(",", ":")))
        return 0
    except Exception as exc:
        log_error("fatal", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
