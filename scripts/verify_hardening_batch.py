"""Post-batch verification for the kraken-bot-hardening dispatch.

Runs after the parallel-codex-runner merges all agents. Verifies that
each spec's acceptance criteria are visibly met. Does NOT place any
live orders; purely introspects the code + bot API state.

Usage:
    python scripts/verify_hardening_batch.py

Exits 0 if all checks pass, 1 if any fail. Prints a compact report.
"""
from __future__ import annotations

import ast
import json
import sys
import urllib.request
from pathlib import Path

RESULTS: list[tuple[str, bool, str]] = []  # (label, ok, detail)


def check(label: str, fn) -> None:
    try:
        ok, detail = fn()
    except Exception as exc:
        ok, detail = False, f"EXC: {exc}"
    RESULTS.append((label, ok, detail))


def _syntax_ok(path: str) -> tuple[bool, str]:
    try:
        ast.parse(Path(path).read_text(encoding="utf-8"))
        return True, "syntax ok"
    except SyntaxError as e:
        return False, f"syntax error: {e}"


# ---------- Spec 01 — floor-round sell qty ----------
def verify_01_floor_round() -> tuple[bool, str]:
    from scripts.cc_brain import _floor_qty  # noqa: F401
    v = _floor_qty(91.5370539700, "CRV/USD")
    if float(v) > 91.5370539700:
        return False, f"_floor_qty overshot: got {v}"
    return True, f"CRV 91.5370539700 -> {v}"


# ---------- Spec 02 — open-orders endpoint ----------
def verify_02_open_orders() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:58392/api/open-orders", timeout=10
        ) as r:
            body = json.load(r)
    except Exception as exc:
        return False, f"endpoint unreachable: {exc}"
    if "orders" not in body or "count" not in body:
        return False, f"missing keys: {list(body.keys())}"
    return True, f"count={body.get('count', 0)}"


def verify_02_helper() -> tuple[bool, str]:
    from scripts.cc_brain import get_pairs_with_open_orders  # noqa: F401
    pairs = get_pairs_with_open_orders()
    return True, f"returned {len(pairs)} pairs"


# ---------- Spec 03 — fiat filter ----------
def verify_03_fiat_filter() -> tuple[bool, str]:
    from scripts.cc_brain import FIAT_CURRENCIES, QUOTE_CURRENCIES
    if "EUR" not in FIAT_CURRENCIES or "AUD" not in FIAT_CURRENCIES:
        return False, "FIAT_CURRENCIES missing expected entries"
    if "USD" in FIAT_CURRENCIES:
        return False, "USD should stay in QUOTE_CURRENCIES, not FIAT_CURRENCIES"
    return True, f"FIAT_CURRENCIES={sorted(FIAT_CURRENCIES)}"


# ---------- Spec 05 — backfill 6h result file ----------
def verify_05_result_file() -> tuple[bool, str]:
    p = Path("tasks/specs/05-backfill-6h-analysis.result.md")
    if not p.exists():
        return False, "file missing"
    size = p.stat().st_size
    return size > 500, f"size={size} bytes"


# ---------- Spec 06 — backfill fidelity filter ----------
def verify_06_backfill_filter() -> tuple[bool, str]:
    src = Path("scripts/backfill_shadow.py").read_text(encoding="utf-8")
    needed = ["Mode:", "DRY RUN", "PLACED:", "FAILED:"]
    missing = [s for s in needed if s not in src]
    if missing:
        return False, f"missing filter tokens: {missing}"
    return True, "filter tokens present in backfill_shadow.py"


# ---------- Spec 07 — ordermin pre-check ----------
def verify_07_ordermin() -> tuple[bool, str]:
    import scripts.cc_brain as m
    m._discovered_cache.clear()
    m._discovered_at = 0
    pairs = m._fetch_kraken_pairs()
    sample = pairs.get("BTC/USD") or pairs.get("ETH/USD") or {}
    if "ordermin" not in sample:
        return False, "ordermin not cached in pair info"
    helper = getattr(m, "_meets_order_minimums", None)
    if helper is None:
        return False, "_meets_order_minimums helper missing"
    ok, reason = helper("BTC/USD", 1e-12, 1.0)
    if ok:
        return False, "tiny-qty check should fail"
    return True, f"helper works, blocked tiny-qty: {reason}"


# ---------- Spec 08 — maker-fee optimization ----------
def verify_08_maker_fee() -> tuple[bool, str]:
    import scripts.cc_brain as m
    entry = getattr(m, "ENTRY_PRICE_BUFFER_BPS", None)
    exit_ = getattr(m, "EXIT_PRICE_BUFFER_BPS", None)
    if entry is None or exit_ is None:
        return False, "ENTRY/EXIT_PRICE_BUFFER_BPS constants missing"
    if entry > 15 or exit_ > 15:
        return False, f"buffer too wide: entry={entry} exit={exit_}"
    buy = getattr(m, "_limit_buy_price", None)
    sell = getattr(m, "_limit_sell_price", None)
    if buy is None or sell is None:
        return False, "_limit_buy_price / _limit_sell_price missing"
    b = buy(100.0, "BTC/USD")
    s = sell(100.0, "BTC/USD")
    if b <= 100.0 - 1e-9 or s >= 100.0 + 1e-9:
        return False, f"limit price math wrong: buy={b} sell={s}"
    return True, f"bps={entry}/{exit_}, @100: buy={b} sell={s}"


# ---------- Spec 09 — USDT investigation result ----------
def verify_09_usdt_result() -> tuple[bool, str]:
    p = Path("tasks/specs/09-usdt-loss-investigation.result.md")
    if not p.exists():
        return False, "result file missing"
    content = p.read_text(encoding="utf-8")
    for required in ("Entry details", "Exit details", "Root cause"):
        if required not in content:
            return False, f"missing section: {required}"
    return True, f"size={p.stat().st_size}"


# ---------- Spec 10 — self-tune rule fix ----------
def verify_10_self_tune() -> tuple[bool, str]:
    import scripts.cc_brain as m
    prev_pos = m.MAX_POSITION_PCT
    prev_thr = m.ENTRY_THRESHOLD
    outcomes = [
        {"net_pnl": 1.0, "fee_total": 0.5, "exit_reason": "timer"},
        {"net_pnl": 1.0, "fee_total": 0.5, "exit_reason": "timer"},
        {"net_pnl": -1.0, "fee_total": 0.5, "exit_reason": "stop_loss"},
        {"net_pnl": -1.0, "fee_total": 0.5, "exit_reason": "stop_loss"},
        {"net_pnl": 1.0, "fee_total": 0.5, "exit_reason": "timer"},
    ]
    m.self_tune(outcomes, [], lambda msg: None)
    if m.MAX_POSITION_PCT != prev_pos:
        return False, (
            f"MAX_POSITION_PCT changed {prev_pos} -> {m.MAX_POSITION_PCT} "
            "(self-tune still bumping the wrong lever)"
        )
    return True, f"MAX_POSITION_PCT={prev_pos} stable; ENTRY_THRESHOLD={m.ENTRY_THRESHOLD}"


def main() -> int:
    # Always syntax-check the files we expect to be modified
    for path in (
        "scripts/cc_brain.py",
        "scripts/backfill_shadow.py",
    ):
        check(f"syntax {path}", lambda p=path: _syntax_ok(p))

    check("01 floor-round", verify_01_floor_round)
    check("02 open-orders endpoint", verify_02_open_orders)
    check("02 get_pairs_with_open_orders helper", verify_02_helper)
    check("03 fiat-filter", verify_03_fiat_filter)
    check("05 backfill result file", verify_05_result_file)
    check("06 backfill filter tokens", verify_06_backfill_filter)
    check("07 ordermin precheck", verify_07_ordermin)
    check("08 maker-fee buffer", verify_08_maker_fee)
    check("09 usdt result file", verify_09_usdt_result)
    check("10 self-tune fee rule", verify_10_self_tune)

    width = max(len(lbl) for lbl, _, _ in RESULTS)
    passed = 0
    for lbl, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {lbl:{width}s}  {detail}")
        if ok:
            passed += 1

    print()
    print(f"  {passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
