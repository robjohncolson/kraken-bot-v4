from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from core.errors import ExchangeError

_ASSET_ALIASES: Final[dict[str, str]] = {
    "BTC": "BTC",
    "DOGE": "DOGE",
    "ETH": "ETH",
    "EUR": "EUR",
    "GBP": "GBP",
    "USD": "USD",
    "USDC": "USDC",
    "USDT": "USDT",
    "XBT": "BTC",
    "XDG": "DOGE",
    "XETH": "ETH",
    "XRP": "XRP",
    "XXBT": "BTC",
    "XXDG": "DOGE",
    "XXRP": "XRP",
    "ZEUR": "EUR",
    "ZGBP": "GBP",
    "ZUSD": "USD",
}
_QUOTE_SUFFIXES: Final[tuple[str, ...]] = (
    "USDT",
    "USDC",
    "ZUSD",
    "ZEUR",
    "ZGBP",
    "USD",
    "EUR",
    "GBP",
    "XXBT",
    "XBT",
    "BTC",
)


class SymbolNormalizationError(ExchangeError):
    """Raised when a Kraken asset or pair cannot be normalized."""

    def __init__(self, raw_symbol: str) -> None:
        self.raw_symbol = raw_symbol
        super().__init__(f"Unable to normalize Kraken symbol {raw_symbol!r}.")


@dataclass(frozen=True, slots=True)
class NormalizedPair:
    base: str
    quote: str

    @property
    def pair(self) -> str:
        return f"{self.base}/{self.quote}"


def normalize_asset_symbol(raw_symbol: str) -> str:
    compact = _compact_symbol(raw_symbol)
    if not compact:
        raise SymbolNormalizationError(raw_symbol)
    return _ASSET_ALIASES.get(compact, compact)


def split_normalized_pair(raw_pair: str) -> NormalizedPair:
    stripped = raw_pair.strip().upper()
    if not stripped:
        raise SymbolNormalizationError(raw_pair)

    for separator in ("/", "-"):
        if separator in stripped:
            base_raw, quote_raw = stripped.split(separator, maxsplit=1)
            if not base_raw or not quote_raw:
                raise SymbolNormalizationError(raw_pair)
            return NormalizedPair(
                base=normalize_asset_symbol(base_raw),
                quote=normalize_asset_symbol(quote_raw),
            )

    compact = _compact_symbol(stripped)
    for quote_suffix in _QUOTE_SUFFIXES:
        if compact.endswith(quote_suffix) and compact != quote_suffix:
            base_raw = compact[: -len(quote_suffix)]
            if base_raw:
                return NormalizedPair(
                    base=normalize_asset_symbol(base_raw),
                    quote=normalize_asset_symbol(quote_suffix),
                )

    raise SymbolNormalizationError(raw_pair)


def normalize_pair(raw_pair: str) -> str:
    return split_normalized_pair(raw_pair).pair


def _compact_symbol(raw_symbol: str) -> str:
    return raw_symbol.strip().upper().replace("/", "").replace("-", "")


__all__ = [
    "NormalizedPair",
    "SymbolNormalizationError",
    "normalize_asset_symbol",
    "normalize_pair",
    "split_normalized_pair",
]
