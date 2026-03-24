from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from fastapi.testclient import TestClient
from starlette.routing import Mount

from web.app import app


STATIC_DIR = Path(__file__).resolve().parents[2] / "web" / "static"


class _DashboardHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.stylesheets: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id is not None:
            self.ids.add(element_id)
        if tag == "link" and attributes.get("rel") == "stylesheet":
            href = attributes.get("href")
            if href is not None:
                self.stylesheets.append(href)
        if tag == "script":
            src = attributes.get("src")
            if src is not None:
                self.scripts.append(src)


def test_static_shell_html_contains_required_dashboard_sections() -> None:
    parser = _DashboardHtmlParser()
    parser.feed((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    assert {
        "app",
        "portfolio-section",
        "portfolio-content",
        "positions-content",
        "grid-section",
        "grid-content",
        "beliefs-section",
        "beliefs-content",
        "stats-section",
        "stats-content",
        "reconciliation-section",
        "reconciliation-content",
        "alerts-section",
        "alerts-content",
        "connection-status",
        "last-event",
    }.issubset(parser.ids)


def test_static_shell_html_references_local_assets_without_d3_modules() -> None:
    parser = _DashboardHtmlParser()
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    parser.feed(html)

    assert parser.stylesheets == ["./styles.css"]
    assert parser.scripts == ["./app.js"]
    assert "d3-" not in html
    assert "d3.js" not in html


def test_app_js_connects_to_sse_and_dispatches_placeholder_updates() -> None:
    javascript = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert 'new EventSource("/sse/updates")' in javascript
    assert "function dispatchUpdate(type, payload, eventId)" in javascript
    assert "function updatePortfolio(payload)" in javascript
    assert "function updateGrid(payload)" in javascript
    assert "function updateBeliefs(payload)" in javascript
    assert "function updateStats(payload)" in javascript
    assert "function updateReconciliation(payload)" in javascript
    assert "function updateAlerts(payload)" in javascript


def test_styles_css_provides_responsive_dashboard_layout() -> None:
    stylesheet = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    assert ":root {" in stylesheet
    assert ".panel-grid {" in stylesheet
    assert ".panel {" in stylesheet
    assert ".hero {" in stylesheet
    assert "@media (max-width: 859px)" in stylesheet


def test_root_static_mount_serves_dashboard_when_present() -> None:
    if not any(
        isinstance(route, Mount) and getattr(route, "path", None) in {"", "/"}
        for route in app.routes
    ):
        return

    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Kraken Bot V4 Dashboard" in response.text
