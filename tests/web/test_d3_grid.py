"""Verify d3-grid.js exists, exports renderGridStatus, and stays under line cap."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
D3_GRID_PATH = REPO_ROOT / "web" / "static" / "d3-grid.js"


def test_d3_grid_file_exists() -> None:
    assert D3_GRID_PATH.exists(), f"Missing: {D3_GRID_PATH}"


def test_d3_grid_exports_render_function() -> None:
    content = D3_GRID_PATH.read_text(encoding="utf-8")
    assert "renderGridStatus" in content


def test_d3_grid_under_line_cap() -> None:
    lines = D3_GRID_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 300, f"d3-grid.js is {len(lines)} lines, cap is 300"


def test_d3_grid_handles_phase_keys() -> None:
    content = D3_GRID_PATH.read_text(encoding="utf-8")
    for phase in ("S0", "S1a", "S1b", "S2"):
        assert phase in content, f"Missing phase key: {phase}"
