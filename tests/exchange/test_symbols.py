from __future__ import annotations

import pytest

from exchange.symbols import (
    NormalizedPair,
    SymbolNormalizationError,
    normalize_asset_symbol,
    normalize_pair,
    split_normalized_pair,
)


@pytest.mark.parametrize(
    ("raw_symbol", "expected"),
    (
        ("XXRP", "XRP"),
        ("XETH", "ETH"),
        ("XDG", "DOGE"),
        ("ZUSD", "USD"),
    ),
)
def test_normalize_asset_symbol_handles_known_kraken_aliases(
    raw_symbol: str,
    expected: str,
) -> None:
    assert normalize_asset_symbol(raw_symbol) == expected


@pytest.mark.parametrize(
    ("raw_pair", "expected"),
    (
        ("xxrpzusd", "XRP/USD"),
        ("xbt/usd", "BTC/USD"),
        ("xdg-usd", "DOGE/USD"),
        ("xethxxbt", "ETH/BTC"),
    ),
)
def test_normalize_pair_handles_compact_and_delimited_formats(
    raw_pair: str,
    expected: str,
) -> None:
    assert normalize_pair(raw_pair) == expected


def test_split_normalized_pair_returns_structured_components() -> None:
    assert split_normalized_pair("xethxxbt") == NormalizedPair(base="ETH", quote="BTC")


def test_normalize_pair_rejects_unrecognized_symbols() -> None:
    with pytest.raises(SymbolNormalizationError):
        normalize_pair("usd")
