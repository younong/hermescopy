from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "skills/productivity/powerpoint/scripts/office/soffice.py"


def _module():
    spec = importlib.util.spec_from_file_location("powerpoint_soffice", WRAPPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_soffice_wrapper_uses_isolated_profile_and_cleans_it(monkeypatch, tmp_path):
    module = _module()
    observed = {}
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(module, "_resolve_soffice", lambda: "/usr/bin/soffice")

    def fake_run(command, *, env, check):
        observed["command"] = command
        observed["env"] = env
        observed["profile"] = Path(command[1].split("file://", 1)[1])
        assert observed["profile"].is_dir()
        assert check is False
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.run_soffice(["--headless", "deck.pptx"]) == 7
    assert observed["command"][0] == "/usr/bin/soffice"
    assert observed["command"][2:] == ["--headless", "deck.pptx"]
    assert observed["env"]["SAL_USE_VCLPLUGIN"] == "svp"
    assert not observed["profile"].exists()


def test_soffice_wrapper_fails_clearly_when_binary_is_missing(monkeypatch, capsys):
    module = _module()
    monkeypatch.setattr(
        module,
        "_resolve_soffice",
        lambda: (_ for _ in ()).throw(FileNotFoundError("missing soffice")),
    )

    assert module.run_soffice(["--headless"]) == 127
    assert "missing soffice" in capsys.readouterr().err
