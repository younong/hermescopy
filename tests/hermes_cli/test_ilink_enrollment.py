"""Tests for durable per-user iLink QR enrollment."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, patch

import pytest

from gateway.weixin_ilink import ILinkCredentials, QRCode, QRCodeStatus, QRStatus
from hermes_cli.channel_connectors.weixin_ilink.enrollment import EnrollmentManager
from hermes_cli.channel_identity import (
    ChannelCrypto,
    ChannelIdentityStore,
    Keyring,
    ensure_owner_binding,
    register_weixin_identity_for_owner,
    resolve_binding,
)
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import owner_context_from_session


def _owner(user_id: str):
    return owner_context_from_session(
        Session(
            user_id=user_id,
            email=f"{user_id}@example.com",
            display_name=user_id,
            org_id="org-a",
            provider="stub",
            expires_at=9_999_999_999,
            access_token="access",
            refresh_token="refresh",
        )
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    return ChannelIdentityStore(
        ChannelCrypto(
            lookup=Keyring(keys={1: b"l" * 32}, active_version=1),
            encryption=Keyring(keys={1: b"e" * 32}, active_version=1),
        )
    )


@pytest.mark.asyncio
async def test_create_persists_waiting_before_return(store):
    manager = EnrollmentManager(store, object(), poll_interval_seconds=60)
    with patch(
        "hermes_cli.channel_connectors.weixin_ilink.enrollment.WeixinILinkClient.create_qr_code",
        new=AsyncMock(return_value=QRCode(token="qr-token", content="https://qr.example/full")),
    ):
        view = await manager.create(source="127.0.0.1", device_id="device", scene="join")

    with store.read() as conn:
        row = conn.execute(
            "SELECT status, qr_ciphertext FROM enrollment_attempts WHERE attempt_id=?",
            (view.attempt_id,),
        ).fetchone()
    assert view.qr_content == "https://qr.example/full"
    assert row["status"] == "waiting"
    assert b"qr-token" not in row["qr_ciphertext"]
    await manager.stop()


@pytest.mark.asyncio
async def test_confirmed_enrollment_consumes_secrets_and_registers_identity(store, tmp_path):
    manager = EnrollmentManager(store, object(), poll_interval_seconds=0)
    credentials = ILinkCredentials(
        bot_id="bot-a",
        bot_token="bot-token",
        base_url="https://ilink.example",
        user_id="peer-a",
    )
    with patch(
        "hermes_cli.channel_connectors.weixin_ilink.enrollment.WeixinILinkClient.create_qr_code",
        new=AsyncMock(return_value=QRCode(token="qr-token", content="https://qr.example/full")),
    ), patch(
        "hermes_cli.channel_connectors.weixin_ilink.enrollment.WeixinILinkClient.get_qr_status",
        new=AsyncMock(return_value=QRStatus(status=QRCodeStatus.CONFIRMED, credentials=credentials)),
    ), patch(
        "hermes_cli.channel_connectors.weixin_ilink.enrollment.ensure_owner_home",
    ):
        view = await manager.create(source="127.0.0.1", device_id="device", scene="join")
        await manager._tasks.copy().pop()

    status = manager.get(view.attempt_id)
    assert status is not None
    assert status.status == "confirmed"
    assert status.next_action == "continue_in_wechat"
    with store.read() as conn:
        attempt = conn.execute(
            "SELECT qr_ciphertext, confirmed_ciphertext, consumed_at FROM enrollment_attempts"
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) AS count FROM canonical_users").fetchone()["count"] == 1
    assert attempt["qr_ciphertext"] is None
    assert attempt["confirmed_ciphertext"] is None
    assert attempt["consumed_at"] is not None
    await manager.stop()


@pytest.mark.asyncio
async def test_identity_stays_pending_until_owner_home_is_provisioned(store):
    manager = EnrollmentManager(store, object(), poll_interval_seconds=0)
    credentials = ILinkCredentials(
        bot_id="bot-pending",
        bot_token="bot-token",
        base_url="https://ilink.example",
        user_id="peer-pending",
    )
    provisioning_started = threading.Event()
    release_provisioning = threading.Event()

    def provision(_owner):
        provisioning_started.set()
        assert release_provisioning.wait(timeout=5)

    with patch(
        "hermes_cli.channel_connectors.weixin_ilink.enrollment.WeixinILinkClient.create_qr_code",
        new=AsyncMock(return_value=QRCode(token="qr-token", content="https://qr.example/full")),
    ), patch(
        "hermes_cli.channel_connectors.weixin_ilink.enrollment.WeixinILinkClient.get_qr_status",
        new=AsyncMock(return_value=QRStatus(status=QRCodeStatus.CONFIRMED, credentials=credentials)),
    ), patch(
        "hermes_cli.channel_connectors.weixin_ilink.enrollment.ensure_owner_home",
        side_effect=provision,
    ):
        view = await manager.create(source="127.0.0.1", device_id="device", scene="join")
        assert await asyncio.to_thread(provisioning_started.wait, 5)
        with store.read() as conn:
            assert conn.execute("SELECT status FROM enrollment_attempts").fetchone()["status"] == "registering"
            assert conn.execute("SELECT status FROM canonical_users").fetchone()["status"] == "pending"
            assert conn.execute("SELECT status FROM ilink_accounts").fetchone()["status"] == "pending"
            assert conn.execute("SELECT status FROM channel_bindings").fetchone()["status"] == "pending"
        release_provisioning.set()
        await manager._tasks.copy().pop()

    assert manager.get(view.attempt_id).status == "confirmed"
    await manager.stop()


@pytest.mark.asyncio
async def test_authenticated_attempt_durably_targets_dashboard_owner(store):
    manager = EnrollmentManager(store, object(), poll_interval_seconds=60)
    owner = _owner("dashboard-user")
    with patch(
        "hermes_cli.channel_connectors.weixin_ilink.enrollment.WeixinILinkClient.create_qr_code",
        new=AsyncMock(return_value=QRCode(token="qr-token", content="full")),
    ):
        view = await manager.create(
            source="127.0.0.1",
            device_id="device",
            scene="internal",
            target_owner=owner,
        )

    with store.read() as conn:
        row = conn.execute(
            """
            SELECT a.target_canonical_user_id, o.owner_key
            FROM enrollment_attempts a
            JOIN owner_bindings o ON o.canonical_user_id=a.target_canonical_user_id
            WHERE a.attempt_id=?
            """,
            (view.attempt_id,),
        ).fetchone()
    assert row["target_canonical_user_id"] != owner.owner_user_id
    assert row["owner_key"] == owner.owner_key
    assert manager.get(view.attempt_id) is None
    assert manager.get(view.attempt_id, target_owner=owner).status == "waiting"
    assert manager.get(view.attempt_id, target_owner=_owner("other-user")) is None
    await manager.stop()


@pytest.mark.asyncio
async def test_authenticated_confirmed_identity_resolves_same_dashboard_owner(store):
    manager = EnrollmentManager(store, object(), poll_interval_seconds=0)
    owner = _owner("dashboard-user")
    credentials = ILinkCredentials(
        bot_id="bot-owner",
        bot_token="bot-token",
        base_url="https://ilink.example",
        user_id="peer-owner",
    )
    with patch(
        "hermes_cli.channel_connectors.weixin_ilink.enrollment.WeixinILinkClient.create_qr_code",
        new=AsyncMock(return_value=QRCode(token="qr-token", content="full")),
    ), patch(
        "hermes_cli.channel_connectors.weixin_ilink.enrollment.WeixinILinkClient.get_qr_status",
        new=AsyncMock(return_value=QRStatus(status=QRCodeStatus.CONFIRMED, credentials=credentials)),
    ), patch(
        "hermes_cli.channel_connectors.weixin_ilink.enrollment.ensure_owner_home",
    ):
        view = await manager.create(
            source="127.0.0.1",
            device_id="device",
            scene="internal",
            target_owner=owner,
        )
        await manager._tasks.copy().pop()

    with store.read() as conn:
        binding_id = conn.execute("SELECT binding_id FROM channel_bindings").fetchone()["binding_id"]
    resolved_owner, _ = resolve_binding(store, binding_id=binding_id)
    assert resolved_owner == owner
    assert manager.get(view.attempt_id, target_owner=owner).status == "confirmed"
    await manager.stop()


@pytest.mark.asyncio
async def test_authenticated_cross_owner_conflict_clears_attempt_secrets(store):
    first_owner = _owner("owner-a")
    second_owner = _owner("owner-b")
    first_target = ensure_owner_binding(store, first_owner)
    register_weixin_identity_for_owner(
        store,
        target_canonical_user_id=first_target,
        subject="peer-conflict",
        bot_id="bot-conflict",
        bot_token="original-token",
        base_url="https://ilink.example",
        peer_id="peer-conflict",
    )
    manager = EnrollmentManager(store, object(), poll_interval_seconds=0)
    credentials = ILinkCredentials(
        bot_id="bot-conflict",
        bot_token="attacker-token",
        base_url="https://attacker.example",
        user_id="peer-conflict",
    )
    with patch(
        "hermes_cli.channel_connectors.weixin_ilink.enrollment.WeixinILinkClient.create_qr_code",
        new=AsyncMock(return_value=QRCode(token="qr-token", content="full")),
    ), patch(
        "hermes_cli.channel_connectors.weixin_ilink.enrollment.WeixinILinkClient.get_qr_status",
        new=AsyncMock(return_value=QRStatus(status=QRCodeStatus.CONFIRMED, credentials=credentials)),
    ):
        view = await manager.create(
            source="127.0.0.1",
            device_id="device",
            scene="internal",
            target_owner=second_owner,
        )
        await manager._tasks.copy().pop()

    with store.read() as conn:
        attempt = conn.execute(
            "SELECT status, qr_ciphertext, confirmed_ciphertext FROM enrollment_attempts WHERE attempt_id=?",
            (view.attempt_id,),
        ).fetchone()
    assert attempt["status"] == "conflict"
    assert attempt["qr_ciphertext"] is None
    assert attempt["confirmed_ciphertext"] is None
    assert manager.get(view.attempt_id, target_owner=second_owner).next_action is None
    await manager.stop()


@pytest.mark.asyncio
async def test_rate_limit_does_not_call_provider(store):
    manager = EnrollmentManager(store, object(), max_events_per_source=1, poll_interval_seconds=60)
    create_qr = AsyncMock(return_value=QRCode(token="qr-token", content="full"))
    with patch(
        "hermes_cli.channel_connectors.weixin_ilink.enrollment.WeixinILinkClient.create_qr_code",
        new=create_qr,
    ):
        await manager.create(source="127.0.0.1", device_id="device-a", scene="join")
        with pytest.raises(RuntimeError, match="rate limit"):
            await manager.create(source="127.0.0.1", device_id="device-b", scene="join")

    assert create_qr.await_count == 1
    await manager.stop()
