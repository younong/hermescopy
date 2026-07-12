"""Tests for the ``hermes checkpoints`` CLI subcommand."""

import argparse

from hermes_cli import checkpoints


def test_clear_uses_status_base_when_store_is_absent(monkeypatch, tmp_path, capsys):
    missing_base = tmp_path / "missing-checkpoints"

    import tools.checkpoint_manager as checkpoint_manager

    monkeypatch.setattr(
        checkpoint_manager,
        "store_status",
        lambda: {
            "base": str(missing_base),
            "total_size_bytes": 0,
            "project_count": 0,
            "legacy_archives": [],
        },
    )
    monkeypatch.setattr(
        checkpoint_manager,
        "clear_all",
        lambda: (_ for _ in ()).throw(AssertionError("clear_all must not run")),
    )

    assert checkpoints.cmd_clear(argparse.Namespace(force=True)) == 0
    assert capsys.readouterr().out == "Nothing to clear — checkpoint base does not exist.\n"
