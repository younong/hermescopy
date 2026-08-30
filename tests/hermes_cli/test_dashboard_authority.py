from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.dashboard_auth.authority import (
    _SCHEMA_VERSION as AUTHORITY_SCHEMA_VERSION,
    AuthorizationScope,
    AuthorityCorrupt,
    AuthorityStore,
    AuthorizationRejected,
    ReaderGenerationState,
    ReaderLeaseState,
    WorkerGenerationState,
    WorkerLeaseState,
)
from hermes_cli.dashboard_auth import ws_tickets
from hermes_cli.dashboard_authority import (
    AuthorityRecoveryError,
    acquire_authority_server_lock,
    authority_status,
    preserve_authority,
    recover_authority,
    recover_continuity,
)
from hermes_cli.sqlite_util import sha256_file


_RECOVERY_GUIDANCE = (
    "Restart cannot recover authority; offline recovery fencing is required."
)


def _scope() -> AuthorizationScope:
    return AuthorizationScope("stub", "tenant", "user", "session", "revision")


def _corrupt_tls_header(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[5:10] = bytes.fromhex("17 03 03 00 13")
    path.write_bytes(data)


def _incident(
    control_home: Path,
) -> tuple[AuthorityStore, Path, dict[str, object], object, object, object]:
    store = AuthorityStore(control_home)
    state = store.activate(_scope())
    worker = store.claim_worker_start("owner", worker_id="worker")
    worker_lease = store.transition_worker_lease(
        worker.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    reader = store.claim_reader_start("owner", reader_id="reader")
    store.transition_reader_lease(
        reader.lease,
        state=ReaderLeaseState.ACTIVE,
        generation_state=ReaderGenerationState.ACTIVE,
    )
    store.check_and_consume(
        _scope(), token_class="ticket", issuer_key_version="key", jti="old-jti",
        audience="browser-ws:/api/ws", expires_at=9999999999,
        claim_epoch=state.epoch, claim_recovery_generation=state.recovery_generation,
    )
    store.check_and_consume_owner_worker_bootstrap(
        worker_lease, issuer_key_version="key", jti="old-worker-jti",
        audience="worker", expires_at=9999999999,
    )
    old_scope_state = store.read_state(_scope())
    old_worker_lease = store.read_owner_worker_lease("owner")
    old_reader_lease = store.read_session_reader_lease("owner")
    assert old_worker_lease is not None
    assert old_reader_lease is not None
    healthy_source = control_home / "healthy-source.sqlite3"
    healthy_source.write_bytes(store.path.read_bytes())
    source = control_home / "source.sqlite3"
    source.write_bytes(healthy_source.read_bytes())
    _corrupt_tls_header(source)
    store.path.write_bytes(source.read_bytes())
    with pytest.raises(AuthorityCorrupt):
        raise store._quarantine_corruption(  # noqa: SLF001 - incident fixture
            "integrity_check returned 'file is not a database'"
        )
    marker = json.loads(store.recovery_marker_path.read_text())
    return store, source, marker, old_scope_state, old_worker_lease, old_reader_lease


def test_status_is_read_only_for_uninitialized_home(tmp_path):
    home = tmp_path / "control-plane"
    status = authority_status(home)
    assert status.state == "uninitialized"
    assert status.recovery_guidance is None
    assert not home.exists()


def test_status_refuses_preexisting_zero_byte_database(tmp_path):
    home = tmp_path / "control-plane"
    home.mkdir(mode=0o700)
    (home / "authority.sqlite3").touch(mode=0o600)

    with pytest.raises(AuthorityRecoveryError, match="status is unavailable"):
        authority_status(home)


def test_status_reports_metadata_only_recovery_required(tmp_path):
    control_home = tmp_path / "control-plane"
    store = AuthorityStore(control_home)
    store.activate(_scope())
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE authority_meta SET value=1 WHERE key='recovery_required'"
        )

    status = authority_status(control_home)

    assert status.state == "recovery_required"
    assert status.schema_version == AUTHORITY_SCHEMA_VERSION
    assert status.recovery_generation == 0
    assert status.sha256 == sha256_file(store.path)
    assert status.size == store.path.stat().st_size
    assert status.incident_id is None
    assert status.classification is None
    assert status.preserved is None
    assert status.recovery_guidance == _RECOVERY_GUIDANCE


@pytest.mark.parametrize(
    "value",
    ["2", "-1", "true", "", 0.5, 1.5, sqlite3.Binary(b"0"), sqlite3.Binary(b"1")],
)
def test_status_rejects_malformed_recovery_required_metadata(tmp_path, value):
    control_home = tmp_path / "control-plane"
    store = AuthorityStore(control_home)
    store.activate(_scope())
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE authority_meta SET value=? WHERE key='recovery_required'", (value,)
        )

    with pytest.raises(AuthorityRecoveryError, match="status is unavailable"):
        authority_status(control_home)


def test_status_marker_includes_recovery_guidance(tmp_path):
    control_home = tmp_path / "control-plane"
    control_home.mkdir(mode=0o700)
    store = AuthorityStore(control_home)
    store.activate(_scope())
    _corrupt_tls_header(store.path)
    with pytest.raises(AuthorityCorrupt):
        raise store._quarantine_corruption(  # noqa: SLF001 - incident fixture
            "integrity_check returned 'file is not a database'"
        )

    status = authority_status(control_home)

    assert status.state == "recovery_required"
    assert status.recovery_guidance == _RECOVERY_GUIDANCE


def test_status_cli_serializes_json_and_text_from_isolated_home(tmp_path):
    hermes_home = tmp_path / "hermes-home"
    control_home = hermes_home / "control-plane"
    store = AuthorityStore(control_home)
    store.activate(_scope())
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE authority_meta SET value=1 WHERE key='recovery_required'"
        )
    env = {**os.environ, "HERMES_HOME": str(hermes_home)}

    json_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "dashboard",
            "authority",
            "status",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    text_result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "dashboard", "authority", "status"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert json_result.returncode == 1, json_result.stderr
    payload = json.loads(json_result.stdout)
    assert payload["state"] == "recovery_required"
    assert payload["schema_version"] == AUTHORITY_SCHEMA_VERSION
    assert payload["recovery_generation"] == 0
    assert payload["sha256"] == sha256_file(store.path)
    assert payload["size"] == store.path.stat().st_size
    assert payload["recovery_guidance"] == _RECOVERY_GUIDANCE
    assert text_result.returncode == 1, text_result.stderr
    assert "state: recovery_required" in text_result.stdout
    assert f"recovery_guidance: {_RECOVERY_GUIDANCE}" in text_result.stdout


def test_status_reports_bound_keyring_mismatch_without_mutating_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    control_home = tmp_path / "control-plane"
    store = AuthorityStore(control_home)
    store.activate(_scope())
    ws_tickets.mint_ticket(user_id="user", provider="stub", store=store)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE authority_meta SET value=1 WHERE key='recovery_generation'")
        conn.execute("UPDATE authority_meta SET value=1 WHERE key='recovery_required'")

    status = authority_status(control_home)

    assert status.state == "recovery_required"
    assert status.keyring_bound is True
    assert status.keyring_recovery_generation == 0
    assert status.recovery_generation == 1
    assert status.continuity_match is False
    assert status.continuity_reason == "recovery_generation_mismatch"


def test_status_reports_missing_bound_keyring_without_mutating_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    control_home = tmp_path / "control-plane"
    store = AuthorityStore(control_home)
    store.activate(_scope())
    ws_tickets.mint_ticket(user_id="user", provider="stub", store=store)
    keyring_path = control_home / "browser_ws_ticket_keyring.json"
    keyring_path.unlink()

    status = authority_status(control_home)

    assert status.state == "recovery_required"
    assert status.keyring_bound is True
    assert status.keyring_state == "unavailable"
    assert status.continuity_reason == "ticket_keyring_unavailable"
    assert store.replay_continuity().recovery_generation == 0


def test_recover_continuity_fences_old_authority_without_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    control_home = tmp_path / "control-plane"
    store = AuthorityStore(control_home)
    store.activate(_scope())
    old_ticket = ws_tickets.mint_ticket(user_id="user", provider="stub", store=store)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE authority_meta SET value=1 WHERE key='recovery_generation'")
        conn.execute("UPDATE authority_meta SET value=1 WHERE key='recovery_required'")

    result = recover_continuity(incident_id="a" * 16, control_home=control_home)

    assert result.state == "healthy"
    assert result.recovery_generation == 2
    assert result.continuity_match is True
    assert result.keyring_recovery_generation == 2
    assert not store.recovery_marker_path.exists()
    assert list(control_home.glob("browser_ws_ticket_keyring.json.continuity.*.bak"))
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT COUNT(*) FROM authorization_scopes WHERE revoked=0").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM consumed_credentials").fetchone() == (0,)
        assert conn.execute("SELECT value FROM authority_meta WHERE key='recovery_required'").fetchone() == (0,)
    with pytest.raises(ws_tickets.TicketInvalid):
        ws_tickets.consume_ticket(old_ticket, store=store)


def test_recover_continuity_refuses_marker_backed_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    control_home = tmp_path / "control-plane"
    store = AuthorityStore(control_home)
    store.activate(_scope())
    _corrupt_tls_header(store.path)
    with pytest.raises(AuthorityCorrupt):
        raise store._quarantine_corruption("integrity_check returned 'file is not a database'")

    with pytest.raises(AuthorityRecoveryError, match="corruption marker"):
        recover_continuity(incident_id="a" * 16, control_home=control_home)


def test_recovery_requires_matching_incident_digest_and_offline_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    control_home = tmp_path / "control-plane"
    control_home.mkdir(mode=0o700)
    ws_tickets.mint_ticket(user_id="user", provider="stub")
    store, source, marker, _, _, _ = _incident(control_home)

    with pytest.raises(AuthorityRecoveryError, match="incident ID"):
        recover_authority(
            incident_id="0" * 16,
            source=source,
            expected_sha256=str(marker["sha256"]),
            repair_tls_offset_5=True,
            control_home=control_home,
        )
    with pytest.raises(AuthorityRecoveryError, match="digest is invalid"):
        recover_authority(
            incident_id=str(marker["incident_id"]),
            source=source,
            expected_sha256="not-a-sha256",
            repair_tls_offset_5=True,
            control_home=control_home,
        )
    with pytest.raises(AuthorityRecoveryError, match="digest mismatch"):
        recover_authority(
            incident_id=str(marker["incident_id"]),
            source=source,
            expected_sha256="0" * 64,
            repair_tls_offset_5=True,
            control_home=control_home,
        )

    lock = acquire_authority_server_lock(control_home)
    try:
        with pytest.raises(AuthorityRecoveryError, match="active dashboard"):
            recover_authority(
                incident_id=str(marker["incident_id"]),
                source=source,
                expected_sha256=str(marker["sha256"]),
                repair_tls_offset_5=True,
                control_home=control_home,
            )
    finally:
        lock.close()
    assert store.recovery_marker_path.exists()


def test_recovery_accepts_separately_digest_pinned_healthy_source(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    control_home = tmp_path / "control-plane"
    control_home.mkdir(mode=0o700)
    ws_tickets.mint_ticket(user_id="user", provider="stub")
    store, _, marker, _, _, _ = _incident(control_home)

    healthy_source = control_home / "healthy-source.sqlite3"
    healthy_digest = sha256_file(healthy_source)
    assert healthy_digest != marker["sha256"]

    result = recover_authority(
        incident_id=str(marker["incident_id"]),
        source=healthy_source,
        expected_sha256=healthy_digest,
        repair_tls_offset_5=False,
        control_home=control_home,
    )

    assert result.state == "healthy"
    assert result.recovery_guidance is None
    assert result.recovery_generation == 1
    assert not store.recovery_marker_path.exists()


def test_recovery_rejects_foreign_authority_source(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    control_home = tmp_path / "control-plane"
    control_home.mkdir(mode=0o700)
    ws_tickets.mint_ticket(user_id="user", provider="stub")
    store, _, marker, _, _, _ = _incident(control_home)
    foreign = AuthorityStore(tmp_path / "foreign-control-plane")
    foreign.activate(_scope())

    with pytest.raises(AuthorityRecoveryError, match="authority identity mismatch"):
        recover_authority(
            incident_id=str(marker["incident_id"]),
            source=foreign.path,
            expected_sha256=sha256_file(foreign.path),
            repair_tls_offset_5=False,
            control_home=control_home,
        )

    assert store.recovery_marker_path.exists()


def test_tls_offset_5_recovery_fences_old_authority_and_clears_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    control_home = tmp_path / "control-plane"
    control_home.mkdir(mode=0o700)
    ws_tickets.mint_ticket(user_id="user", provider="stub")
    (
        store,
        source,
        marker,
        old_scope_state,
        old_worker_lease,
        old_reader_lease,
    ) = _incident(control_home)

    live_stat = store.path.stat()
    chown_calls = []
    real_chown = os.chown

    def _record_chown(path, uid, gid):
        chown_calls.append((Path(path), uid, gid))
        real_chown(path, uid, gid)

    monkeypatch.setattr("hermes_cli.dashboard_authority.os.chown", _record_chown)
    result = recover_authority(
        incident_id=str(marker["incident_id"]),
        source=source,
        expected_sha256=str(marker["sha256"]),
        repair_tls_offset_5=True,
        control_home=control_home,
    )

    assert result.state == "healthy"
    assert any(
        path.name.startswith(".authority-recovery-built.")
        and (uid, gid) == (live_stat.st_uid, live_stat.st_gid)
        for path, uid, gid in chown_calls
    )
    recovered_stat = store.path.stat()
    assert (recovered_stat.st_uid, recovered_stat.st_gid) == (
        live_stat.st_uid,
        live_stat.st_gid,
    )
    assert recovered_stat.st_mode & 0o777 == live_stat.st_mode & 0o777
    assert result.recovery_generation == 1
    assert not store.recovery_marker_path.exists()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT COUNT(*) FROM authorization_scopes WHERE revoked=0").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM consumed_credentials").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM owner_worker_bootstrap_consumptions").fetchone() == (0,)
        assert {row[0] for row in conn.execute("SELECT state FROM owner_worker_leases")} == {"revoked"}
        assert {row[0] for row in conn.execute("SELECT state FROM session_reader_leases")} == {"revoked"}
    keyring = json.loads((control_home / "browser_ws_ticket_keyring.json").read_text())
    assert keyring["replay_continuity"]["recovery_generation"] == 1
    assert keyring["replay_continuity"]["state"] == "ready"
    recovered_store = AuthorityStore(control_home)
    with pytest.raises(AuthorizationRejected, match="session_revoked"):
        recovered_store.check_and_consume(
            _scope(), token_class="ticket", issuer_key_version="key", jti="fresh-jti",
            audience="browser-ws:/api/ws", expires_at=9999999999,
            claim_epoch=old_scope_state.epoch,
            claim_recovery_generation=old_scope_state.recovery_generation,
        )
    with pytest.raises(AuthorizationRejected, match="stale"):
        recovered_store.assert_worker_lease(old_worker_lease)
    with pytest.raises(AuthorizationRejected, match="stale"):
        recovered_store.assert_reader_lease(old_reader_lease)


def test_preserve_retries_failed_evidence_without_changing_incident(tmp_path, monkeypatch):
    control_home = tmp_path / "control-plane"
    control_home.mkdir(mode=0o700)
    store = AuthorityStore(control_home)
    store.activate(_scope())
    _corrupt_tls_header(store.path)
    monkeypatch.setattr(
        "hermes_cli.dashboard_auth.authority.copy_sqlite_forensics",
        lambda _path: None,
    )
    with pytest.raises(AuthorityCorrupt):
        raise store._quarantine_corruption(  # noqa: SLF001 - incident fixture
            "integrity_check returned 'file is not a database'"
        )
    original = json.loads(store.recovery_marker_path.read_text())
    assert original["preserved"] is False

    monkeypatch.undo()
    result = preserve_authority(control_home)

    updated = json.loads(store.recovery_marker_path.read_text())
    assert result.state == "recovery_required"
    assert result.recovery_guidance == _RECOVERY_GUIDANCE
    assert result.preserved is True
    assert updated["incident_id"] == original["incident_id"]
    assert updated["detected_at"] == original["detected_at"]
    assert list(control_home.glob("authority.sqlite3.corrupt.*.bak"))


def test_recovery_refuses_symlink_source(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    control_home = tmp_path / "control-plane"
    control_home.mkdir(mode=0o700)
    ws_tickets.mint_ticket(user_id="user", provider="stub")
    store, source, marker, _, _, _ = _incident(control_home)
    linked = control_home / "linked-source.sqlite3"
    linked.symlink_to(source)

    with pytest.raises(AuthorityRecoveryError, match="regular file"):
        recover_authority(
            incident_id=str(marker["incident_id"]),
            source=linked,
            expected_sha256=str(marker["sha256"]),
            repair_tls_offset_5=True,
            control_home=control_home,
        )
    assert store.recovery_marker_path.exists()


def test_recovery_refuses_non_exact_tls_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    control_home = tmp_path / "control-plane"
    control_home.mkdir(mode=0o700)
    ws_tickets.mint_ticket(user_id="user", provider="stub")
    store, source, marker, _, _, _ = _incident(control_home)
    data = bytearray(source.read_bytes())
    data[9] ^= 1
    source.write_bytes(data)
    digest = sha256_file(source)
    marker["sha256"] = digest
    marker["incident_id"] = digest[:16]
    store.recovery_marker_path.write_text(json.dumps(marker))

    with pytest.raises(AuthorityRecoveryError, match="exact TLS"):
        recover_authority(
            incident_id=digest[:16], source=source, expected_sha256=digest,
            repair_tls_offset_5=True, control_home=control_home,
        )
    assert store.recovery_marker_path.exists()
