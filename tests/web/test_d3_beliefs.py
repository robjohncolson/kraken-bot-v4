"""Verify d3-beliefs.js exists, exports renderBeliefMatrix, and handles empty data."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
D3_BELIEFS_PATH = REPO_ROOT / "web" / "static" / "d3-beliefs.js"


def _content() -> str:
    return D3_BELIEFS_PATH.read_text(encoding="utf-8")


def test_d3_beliefs_file_exists() -> None:
    assert D3_BELIEFS_PATH.exists(), f"Missing: {D3_BELIEFS_PATH}"


def test_d3_beliefs_exports_render_function() -> None:
    content = _content()
    assert "module.exports" in content
    assert "renderBeliefMatrix" in content


def test_d3_beliefs_under_line_cap() -> None:
    assert len(_content().splitlines()) <= 300


def test_d3_beliefs_handles_empty_and_null_data_gracefully() -> None:
    content = _content()
    assert 'if (value === null || value === undefined) return {};' in content
    assert 'container.textContent = "No belief data";' in content


def test_d3_beliefs_bridges_sse_placeholder_updates_incrementally() -> None:
    content = _content()
    assert 'targetId === "beliefs-content"' in content
    assert 'renderBeliefMatrix(payload, targetId);' in content
    assert 'selectAll("g.belief-cell")' in content
