from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER_PATH = REPO_ROOT / "scripts" / "dev_loop.ps1"


def test_dev_loop_wrapper_parses_in_pwsh() -> None:
    if shutil.which("pwsh") is None:
        pytest.skip("pwsh is not available")

    command = [
        "pwsh",
        "-NoProfile",
        "-Command",
        f"[scriptblock]::Create((Get-Content -Raw '{WRAPPER_PATH.as_posix()}')) | Out-Null",
    ]
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        "PowerShell failed to parse scripts/dev_loop.ps1\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
