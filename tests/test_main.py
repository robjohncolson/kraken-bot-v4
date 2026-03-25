from __future__ import annotations

import os
import sys
import types

import main


def test_load_dotenv_does_not_override_existing_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("STARTUP_RECONCILE_ONLY=true\n", encoding="utf-8")
    monkeypatch.setenv("STARTUP_RECONCILE_ONLY", "false")

    called: dict[str, object] = {}

    def fake_load_dotenv(path, override: bool = False) -> None:
        called["path"] = path
        called["override"] = override
        if override or "STARTUP_RECONCILE_ONLY" not in os.environ:
            os.environ["STARTUP_RECONCILE_ONLY"] = "true"

    monkeypatch.setitem(
        sys.modules,
        "dotenv",
        types.SimpleNamespace(load_dotenv=fake_load_dotenv),
    )

    main._load_dotenv()

    assert os.environ["STARTUP_RECONCILE_ONLY"] == "false"
    assert called["override"] is False
