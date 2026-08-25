from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hermes_cli.owner_worker import entrypoint
from hermes_cli.owner_worker.tokens import OWP1_MAX_MESSAGE_BYTES


def test_run_worker_sets_bounded_websocket_size(monkeypatch, tmp_path):
    socket_path = tmp_path / "worker.sock"
    args = SimpleNamespace(
        owner_key="ok1_owner",
        owner_home=str(tmp_path),
        socket=str(socket_path),
        tenant_id="tenant",
        owner_user_id="user",
        auth_provider="test",
        control_home="",
        worker_generation=1,
        worker_id="worker-1",
    )
    captured = {}
    monkeypatch.setattr(entrypoint, "_parse_args", lambda: args)
    monkeypatch.setattr(
        entrypoint,
        "_prepare_owner_env",
        lambda _args: ("ok1_owner", tmp_path, socket_path),
    )
    monkeypatch.setattr(entrypoint, "create_app", lambda *_args, **_kwargs: "app")

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(app=app, **kwargs))

    entrypoint.run_worker()

    assert captured["app"] == "app"
    assert captured["uds"] == str(Path(socket_path))
    assert captured["ws_max_size"] == OWP1_MAX_MESSAGE_BYTES
