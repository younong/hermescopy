"""Tests for durable per-user iLink QR enrollment."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, patch

import pytest

from gateway.weixin_ilink import ILinkCredentials, QRCode, QRCodeStatus, QRStatus
from hermes_cli.channel_connectors.weixin_ilink.enrollment import EnrollmentManager
from hermes_cli.channel_identity import ChannelCrypto, ChannelIdentityStore, Keyring


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
