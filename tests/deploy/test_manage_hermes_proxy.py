from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


MODULE_PATH = Path(__file__).parents[2] / "deploy" / "nginx" / "manage_hermes_proxy.py"
SPEC = importlib.util.spec_from_file_location("manage_hermes_proxy", MODULE_PATH)
assert SPEC and SPEC.loader
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


LEGACY = """server {
    server_name abinllm.xyz;

    location = /__hermes_remember_check {
        internal;
        if ($cookie_hermes_remember = "opaque") { return 204; }
        return 401;
    }

    location = /hermes {
        return 301 /hermes/;
    }

    location /hermes/api/ {
        auth_basic off;
        proxy_pass http://127.0.0.1:9119/api/;
    }

    location /hermes/ {
        satisfy any;
        auth_request /__hermes_remember_check;
        auth_basic "Hermes Dashboard";
        auth_basic_user_file /etc/nginx/.htpasswd-hermes;
        proxy_pass http://127.0.0.1:9119/;
    }

    location / {
        proxy_pass http://127.0.0.1:4000;
    }
    # Certbot-owned marker
}
"""


def test_migrate_text_replaces_only_legacy_hermes_locations():
    migrated = proxy.migrate_text(LEGACY)
    assert migrated.count(proxy.DEFAULT_INCLUDE) == 1
    assert "auth_basic \"Hermes Dashboard\"" not in migrated
    assert "__hermes_remember_check" not in migrated
    assert "proxy_pass http://127.0.0.1:4000;" in migrated
    assert "# Certbot-owned marker" in migrated
    untouched_prefix = LEGACY[: LEGACY.index("    location = /__hermes_remember_check")]
    untouched_suffix = LEGACY[LEGACY.index("    location / {") :]
    assert migrated.startswith(untouched_prefix)
    assert migrated.endswith(untouched_suffix)
    assert proxy.migration_status(migrated) == "current"


@pytest.mark.parametrize(
    "text",
    [
        LEGACY.replace("auth_request /__hermes_remember_check;", ""),
        LEGACY + "\nlocation /hermes/ { proxy_pass http://127.0.0.1:9119/; }\n",
        LEGACY.replace(
            "location /hermes/ {", f"{proxy.DEFAULT_INCLUDE}\n    location /hermes/ {{"
        ),
        LEGACY.replace(
            "server_name abinllm.xyz;",
            f"server_name abinllm.xyz;\n    {proxy.DEFAULT_INCLUDE}\n    {proxy.DEFAULT_INCLUDE}",
        ),
    ],
)
def test_migration_rejects_unknown_duplicate_or_partial_shapes(text):
    with pytest.raises(proxy.ProxyConfigError):
        proxy.migrate_text(text)


def test_status_action_does_not_require_snippet_source(tmp_path):
    vhost = tmp_path / "site.conf"
    vhost.write_text(f"server {{\n    {proxy.DEFAULT_INCLUDE}\n}}\n")
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "status", "--vhost", str(vhost)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "current"


def test_reconcile_is_idempotent(tmp_path, monkeypatch):
    vhost = tmp_path / "site.conf"
    source = tmp_path / "source.conf"
    target = tmp_path / "installed.conf"
    vhost.write_text(f"server {{\n    {proxy.DEFAULT_INCLUDE}\n}}\n")
    source.write_text("location /hermes/ { auth_basic off; }\n")
    target.write_text(source.read_text())
    calls = []
    monkeypatch.setattr(proxy, "_validate", lambda nginx: calls.append(nginx))
    monkeypatch.setattr(proxy, "_reload", lambda: calls.append("reload"))

    assert proxy.reconcile(
        vhost=vhost,
        snippet_source=source,
        snippet_target=target,
        nginx="nginx",
    ) is False
    assert calls == []


def test_migrate_restores_vhost_and_snippet_when_validation_fails(
    tmp_path, monkeypatch
):
    vhost = tmp_path / "site.conf"
    source = tmp_path / "source.conf"
    target = tmp_path / "installed.conf"
    vhost.write_text(LEGACY)
    source.write_text("new\n")
    target.write_text("old\n")

    validations = 0

    def validate(_nginx):
        nonlocal validations
        validations += 1
        if validations == 1:
            raise RuntimeError("nginx -t failed")

    monkeypatch.setattr(proxy, "_validate", validate)
    monkeypatch.setattr(proxy, "_reload", lambda: None)
    with pytest.raises(RuntimeError, match="nginx -t failed"):
        proxy.migrate(
            vhost=vhost,
            snippet_source=source,
            snippet_target=target,
            nginx="nginx",
        )
    assert vhost.read_text() == LEGACY
    assert target.read_text() == "old\n"
    assert list(tmp_path.glob("site.conf.hermes-backup-*"))


def test_reconcile_restores_loaded_config_when_reload_fails(tmp_path, monkeypatch):
    vhost = tmp_path / "site.conf"
    source = tmp_path / "source.conf"
    target = tmp_path / "installed.conf"
    vhost.write_text(f"server {{\n    {proxy.DEFAULT_INCLUDE}\n}}\n")
    source.write_text("new\n")
    target.write_text("old\n")
    reloads = 0

    def reload_nginx():
        nonlocal reloads
        reloads += 1
        if reloads == 1:
            raise RuntimeError("reload failed")

    monkeypatch.setattr(proxy, "_validate", lambda _nginx: None)
    monkeypatch.setattr(proxy, "_reload", reload_nginx)
    with pytest.raises(RuntimeError, match="reload failed"):
        proxy.reconcile(
            vhost=vhost,
            snippet_source=source,
            snippet_target=target,
            nginx="nginx",
        )
    assert target.read_text() == "old\n"
    assert reloads == 2


def test_reconcile_restores_snippet_when_validation_fails(tmp_path, monkeypatch):
    vhost = tmp_path / "site.conf"
    source = tmp_path / "source.conf"
    target = tmp_path / "installed.conf"
    vhost.write_text(f"server {{\n    {proxy.DEFAULT_INCLUDE}\n}}\n")
    source.write_text("new\n")
    target.write_text("old\n")

    validations = 0

    def validate(_nginx):
        nonlocal validations
        validations += 1
        if validations == 1:
            raise RuntimeError("nginx -t failed")

    monkeypatch.setattr(proxy, "_validate", validate)
    monkeypatch.setattr(proxy, "_reload", lambda: None)
    with pytest.raises(RuntimeError, match="nginx -t failed"):
        proxy.reconcile(
            vhost=vhost,
            snippet_source=source,
            snippet_target=target,
            nginx="nginx",
        )
    assert target.read_text() == "old\n"


def test_repository_snippet_has_single_auth_layer_contract():
    snippet = (Path(__file__).parents[2] / "deploy" / "nginx" / "hermes-dashboard.conf").read_text()
    assert 'auth_basic "' not in snippet
    assert "auth_basic off;" in snippet
    assert "auth_request off;" in snippet
    assert "auth_request /" not in snippet
    assert "proxy_pass http://127.0.0.1:9119/;" in snippet
    assert "proxy_set_header Host $host;" in snippet
    assert "proxy_set_header X-Forwarded-Prefix /hermes;" in snippet
    assert 'proxy_set_header Upgrade $http_upgrade;' in snippet
    assert 'proxy_set_header Connection "upgrade";' in snippet
    assert "proxy_read_timeout 3600s;" in snippet
    assert "proxy_send_timeout 3600s;" in snippet
