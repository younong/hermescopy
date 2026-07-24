"""Durable, short-lived per-user iLink QR enrollments."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass

from gateway.weixin_ilink import QRCodeStatus, WeixinILinkClient
from hermes_cli.channel_identity.registration import (
    ChannelIdentityOwnershipConflict,
    activate_weixin_identity,
    ensure_owner_binding,
    register_weixin_identity,
    register_weixin_identity_for_owner,
)
from hermes_cli.channel_identity.store import ChannelIdentityStore
from hermes_cli.dashboard_auth.owner_context import OwnerContext, ensure_owner_home
from hermes_cli.channel_identity.owner_resolution import resolve_binding


@dataclass(frozen=True)
class EnrollmentView:
    attempt_id: str
    status: str
    expires_at: float
    qr_content: str | None = None
    next_action: str | None = None


class EnrollmentManager:
    def __init__(
        self,
        store: ChannelIdentityStore,
        session,
        *,
        bot_type: str = "3",
        ttl_seconds: int = 480,
        poll_interval_seconds: float = 1.0,
        max_pending_global: int = 100,
        max_events_per_source: int = 5,
        rate_window_seconds: int = 300,
        on_account_activated=None,
    ) -> None:
        self.store = store
        self.session = session
        self.bot_type = bot_type
        self.ttl_seconds = ttl_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.max_pending_global = max_pending_global
        self.max_events_per_source = max_events_per_source
        self.rate_window_seconds = rate_window_seconds
        self.on_account_activated = on_account_activated
        self._tasks: set[asyncio.Task] = set()
        self._accepting = True

    async def create(
        self,
        *,
        source: str,
        device_id: str,
        scene: str,
        target_owner: OwnerContext | None = None,
    ) -> EnrollmentView:
        if not self._accepting:
            raise RuntimeError("enrollment service is stopping")
        if scene not in {"join", "invite", "internal"}:
            raise ValueError("unsupported enrollment scene")
        if not device_id or len(device_id) > 128:
            raise ValueError("device_id is required and must be at most 128 characters")
        now = time.time()
        attempt_id = f"enr_{uuid.uuid4().hex}"
        source_hash = self.store.crypto.lookup_hash("enrollment-source", source)
        device_hash = self.store.crypto.lookup_hash("enrollment-device", device_id)
        with self.store.write() as conn:
            target_canonical_user_id = (
                ensure_owner_binding(self.store, target_owner, conn=conn)
                if target_owner is not None
                else None
            )
            conn.execute(
                "DELETE FROM enrollment_rate_events WHERE occurred_at < ?",
                (now - self.rate_window_seconds,),
            )
            count = conn.execute(
                """
                SELECT COUNT(*) AS count FROM enrollment_rate_events
                WHERE source_lookup_hash=? OR device_lookup_hash=?
                """,
                (source_hash, device_hash),
            ).fetchone()["count"]
            pending = conn.execute(
                "SELECT COUNT(*) AS count FROM enrollment_attempts WHERE status IN ('creating','waiting','scanned','registering') AND expires_at>?",
                (now,),
            ).fetchone()["count"]
            if count >= self.max_events_per_source or pending >= self.max_pending_global:
                raise RuntimeError("enrollment rate limit exceeded")
            conn.execute(
                "INSERT INTO enrollment_rate_events(source_lookup_hash, device_lookup_hash, occurred_at) VALUES (?, ?, ?)",
                (source_hash, device_hash, now),
            )
            conn.execute(
                """
                INSERT INTO enrollment_attempts
                  (attempt_id, status, scene, source_lookup_hash, device_lookup_hash,
                   target_canonical_user_id, expires_at, next_poll_at, created_at, updated_at)
                VALUES (?, 'creating', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    scene,
                    source_hash,
                    device_hash,
                    target_canonical_user_id,
                    now + self.ttl_seconds,
                    now,
                    now,
                    now,
                ),
            )
        try:
            qr = await WeixinILinkClient(self.session).create_qr_code(bot_type=self.bot_type)
        except Exception:
            self._set_terminal(attempt_id, "failed")
            raise
        encrypted, version = self.store.crypto.encrypt_text(
            json.dumps({"token": qr.token, "content": qr.content}),
            table="enrollment_attempts",
            record_id=attempt_id,
            field="qr",
        )
        with self.store.write() as conn:
            changed = conn.execute(
                """
                UPDATE enrollment_attempts
                SET status='waiting', qr_ciphertext=?, qr_key_version=?, updated_at=?
                WHERE attempt_id=? AND status='creating' AND expires_at>?
                """,
                (encrypted, version, time.time(), attempt_id, time.time()),
            ).rowcount
            if changed != 1:
                raise RuntimeError("enrollment attempt expired before QR activation")
        task = asyncio.create_task(self._poll(attempt_id), name=f"ilink-enrollment-{attempt_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return EnrollmentView(
            attempt_id=attempt_id,
            status="waiting",
            expires_at=now + self.ttl_seconds,
            qr_content=qr.content,
        )

    def get(
        self,
        attempt_id: str,
        *,
        target_owner: OwnerContext | None = None,
    ) -> EnrollmentView | None:
        target_canonical_user_id = None
        if target_owner is not None:
            with self.store.read() as conn:
                target = conn.execute(
                    """
                    SELECT o.canonical_user_id, o.auth_provider, o.tenant_id,
                           o.owner_user_id, u.status
                    FROM owner_bindings o
                    JOIN canonical_users u ON u.canonical_user_id=o.canonical_user_id
                    WHERE o.owner_key=?
                    """,
                    (target_owner.owner_key,),
                ).fetchone()
            if target is None or target["status"] != "active" or (
                target["auth_provider"] != target_owner.auth_provider
                or target["tenant_id"] != target_owner.tenant_id
                or target["owner_user_id"] != target_owner.owner_user_id
            ):
                return None
            target_canonical_user_id = target["canonical_user_id"]
        with self.store.read() as conn:
            row = conn.execute(
                """
                SELECT status, expires_at FROM enrollment_attempts
                WHERE attempt_id=? AND target_canonical_user_id IS ?
                """,
                (attempt_id, target_canonical_user_id),
            ).fetchone()
        if row is None:
            return None
        status = row["status"]
        actions = {
            "confirmed": "continue_in_wechat",
            "expired": "retry",
            "failed": "retry",
            "conflict": None,
        }
        return EnrollmentView(
            attempt_id=attempt_id,
            status=status,
            expires_at=row["expires_at"],
            next_action=actions.get(status),
        )

    async def _poll(self, attempt_id: str) -> None:
        while self._accepting:
            with self.store.read() as conn:
                row = conn.execute(
                    "SELECT status, qr_ciphertext, qr_key_version, expires_at FROM enrollment_attempts WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()
            if row is None or row["status"] not in {"waiting", "scanned"}:
                return
            if row["expires_at"] <= time.time():
                self._set_terminal(attempt_id, "expired")
                return
            qr_payload = json.loads(
                self.store.crypto.decrypt_text(
                    row["qr_ciphertext"],
                    table="enrollment_attempts",
                    record_id=attempt_id,
                    field="qr",
                    version=row["qr_key_version"],
                )
            )
            try:
                status = await WeixinILinkClient(self.session).get_qr_status(qr_payload["token"])
            except asyncio.TimeoutError:
                await asyncio.sleep(self.poll_interval_seconds)
                continue
            if status.status is QRCodeStatus.WAITING:
                pass
            elif status.status is QRCodeStatus.SCANNED:
                self._compare_and_set(attempt_id, {"waiting", "scanned"}, "scanned")
            elif status.status is QRCodeStatus.REDIRECT:
                if status.redirect_host:
                    qr_payload["base_url"] = f"https://{status.redirect_host}"
            elif status.status is QRCodeStatus.EXPIRED:
                self._set_terminal(attempt_id, "expired")
                return
            elif status.status is QRCodeStatus.CONFIRMED:
                assert status.credentials is not None
                await self._register(attempt_id, status.credentials)
                return
            await asyncio.sleep(self.poll_interval_seconds)

    async def _register(self, attempt_id: str, credentials) -> None:
        confirmed, version = self.store.crypto.encrypt_text(
            json.dumps(
                {
                    "bot_id": credentials.bot_id,
                    "bot_token": credentials.bot_token,
                    "base_url": credentials.base_url,
                    "user_id": credentials.user_id,
                }
            ),
            table="enrollment_attempts",
            record_id=attempt_id,
            field="confirmed",
        )
        with self.store.write() as conn:
            changed = conn.execute(
                """
                UPDATE enrollment_attempts
                SET status='registering', confirmed_ciphertext=?, confirmed_key_version=?, updated_at=?
                WHERE attempt_id=? AND status IN ('waiting','scanned') AND expires_at>?
                """,
                (confirmed, version, time.time(), attempt_id, time.time()),
            ).rowcount
        if changed != 1:
            return
        try:
            with self.store.read() as conn:
                attempt = conn.execute(
                    "SELECT target_canonical_user_id FROM enrollment_attempts WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()
            if attempt is None:
                return
            target_canonical_user_id = attempt["target_canonical_user_id"]
            if target_canonical_user_id is None:
                registered = register_weixin_identity(
                    self.store,
                    subject=credentials.user_id,
                    bot_id=credentials.bot_id,
                    bot_token=credentials.bot_token,
                    base_url=credentials.base_url,
                    peer_id=credentials.user_id,
                    activate=False,
                )
            else:
                registered = register_weixin_identity_for_owner(
                    self.store,
                    target_canonical_user_id=target_canonical_user_id,
                    subject=credentials.user_id,
                    bot_id=credentials.bot_id,
                    bot_token=credentials.bot_token,
                    base_url=credentials.base_url,
                    peer_id=credentials.user_id,
                    activate=False,
                )
            owner, _ = resolve_binding(
                self.store,
                binding_id=registered.binding_id,
                allow_pending=True,
            )
            if (
                target_canonical_user_id is not None
                and registered.canonical_user_id != target_canonical_user_id
            ):
                raise RuntimeError("pending channel registration changed during provisioning")
            await asyncio.to_thread(ensure_owner_home, owner)
            activate_weixin_identity(self.store, registered=registered)
        except ChannelIdentityOwnershipConflict:
            self._set_terminal(attempt_id, "conflict")
            return
        except Exception:
            self._set_terminal(attempt_id, "failed")
            raise
        with self.store.write() as conn:
            changed = conn.execute(
                """
                UPDATE enrollment_attempts
                SET status='confirmed', consumed_at=?, qr_ciphertext=NULL, qr_key_version=NULL,
                    confirmed_ciphertext=NULL, confirmed_key_version=NULL, updated_at=?
                WHERE attempt_id=? AND status='registering'
                """,
                (time.time(), time.time(), attempt_id),
            ).rowcount
        if changed == 1 and self.on_account_activated is not None:
            await self.on_account_activated()

    def _compare_and_set(self, attempt_id: str, expected: set[str], status: str) -> None:
        placeholders = ",".join("?" for _ in expected)
        with self.store.write() as conn:
            conn.execute(
                f"UPDATE enrollment_attempts SET status=?, updated_at=? WHERE attempt_id=? AND status IN ({placeholders})",
                (status, time.time(), attempt_id, *sorted(expected)),
            )

    def _set_terminal(self, attempt_id: str, status: str) -> None:
        with self.store.write() as conn:
            conn.execute(
                """
                UPDATE enrollment_attempts SET status=?, qr_ciphertext=NULL, qr_key_version=NULL,
                    confirmed_ciphertext=NULL, confirmed_key_version=NULL, updated_at=?
                WHERE attempt_id=? AND status NOT IN ('confirmed','expired','failed','conflict')
                """,
                (status, time.time(), attempt_id),
            )

    async def stop(self) -> None:
        self._accepting = False
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
