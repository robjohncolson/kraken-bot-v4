"""Holdings table widget — all Kraken holdings."""
from __future__ import annotations

from rich.text import Text
from textual.widgets import DataTable

from tui.theme import HEALTHY, MUTED, UNHEALTHY, WARNING

_COLUMNS = ("Asset", "Qty", "Price", "USD Value", "%Port", "Stability", "Shadow")


def _stability_text(stability: float | None, asset: str) -> Text:
    """Color-coded stability display."""
    if stability is None:
        return Text("-", style=MUTED)
    if stability > 0.7:
        style = HEALTHY
    elif stability >= 0.4:
        style = WARNING
    else:
        style = UNHEALTHY
    return Text(f"{stability:.2f}", style=style)


def _shadow_text(shadow: dict | None) -> Text:
    """Color-coded shadow top3_mean display.

    Eligible (n >= 3): score colored by range.
    Insufficient (n >= 1 but < 3): muted "n=X".
    No data: muted "-".
    """
    if not shadow:
        return Text("-", style=MUTED)
    if not shadow.get("eligible"):
        n = shadow.get("n", 0)
        return Text(f"n={n}" if n else "-", style=MUTED)
    top3m = shadow.get("top3m")
    if top3m is None:
        return Text("-", style=MUTED)
    if top3m > 0.65:
        style = HEALTHY
    elif top3m >= 0.45:
        style = WARNING
    else:
        style = UNHEALTHY
    return Text(f"{top3m:.2f}", style=style)


class HoldingsTable(DataTable):
    """Tabular display of all Kraken holdings."""

    DEFAULT_CSS = """
    HoldingsTable {
        border: solid $accent;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "HOLDINGS"
        self.cursor_type = "row"
        self.zebra_stripes = True
        for col in _COLUMNS:
            self.add_column(col, key=col.lower().replace(" ", "_").replace("%", "pct"))

    def refresh_content(
        self,
        holdings: list[dict],
        portfolio_value_usd: str,
    ) -> None:
        self.clear()
        if not holdings:
            self.add_row("\u2014", "", "", "", "", "", "")
            self.border_subtitle = ""
            return

        try:
            total = float(portfolio_value_usd)
        except (ValueError, TypeError):
            total = 0.0

        # Sort by USD value descending
        sorted_holdings = sorted(
            holdings,
            key=lambda h: float(h.get("usd_value", 0)),
            reverse=True,
        )

        for h in sorted_holdings:
            asset = h.get("asset", "?")
            qty = h.get("quantity", "0")
            price = h.get("price", "0")

            try:
                usd_val = float(h.get("usd_value", 0))
            except (ValueError, TypeError):
                usd_val = 0.0

            pct = (usd_val / total * 100) if total > 0 else 0.0
            stability = h.get("stability")
            if stability is not None:
                try:
                    stability = float(stability)
                except (ValueError, TypeError):
                    stability = None

            self.add_row(
                asset,
                qty,
                f"${price}",
                f"${usd_val:,.2f}",
                f"{pct:.1f}%",
                _stability_text(stability, asset),
                _shadow_text(h.get("shadow")),
            )

        n = len(sorted_holdings)
        self.border_subtitle = (
            f"Total: ${total:,.2f} across {n} assets"
        )
