"""Strict real-path integration for central iLink dispatch through Owner Workers."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gateway.weixin_ilink import ILinkCredentials
from hermes_cli.channel_connectors.weixin_ilink.enrollment import EnrollmentManager
from hermes_cli.channel_connectors.weixin_ilink.poller import (
    AccountPoller,
    acquire_poll_lease,
    commit_update_batch,
)
from hermes_cli.channel_connectors.weixin_ilink.sender import OutboundSender, claim_outbound
from hermes_cli.channel_dispatch import ChannelDispatcher
from hermes_cli.channel_identity import ChannelCrypto, ChannelIdentityStore, Keyring, resolve_binding
from hermes_cli.deployment_inference import DeploymentInferencePolicy
from hermes_cli.owner_worker import OwnerWorkerSupervisor


class _Response:
    def __init__(self, payload: dict, *, status: int = 200) -> None:
        self._payload = payload
        self.status = status
        self.ok = 200 <= status < 300

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def text(self) -> str:
        return json.dumps(self._payload)


class _FakeILinkSession:
    def __init__(self) -> None:
        self.qr_credentials: list[ILinkCredentials] = []
        self.qr_by_token: dict[str, ILinkCredentials] = {}
        self.update_batches: list[dict] = []
        self.sent_messages: list[dict] = []
        self.fail_next_send = False

    def get(self, url: str, **_kwargs):
        if "get_bot_qrcode" in url:
            credentials = self.qr_credentials.pop(0)
            token = f"qr-{credentials.user_id}"
            self.qr_by_token[token] = credentials
            return _Response(
                {
                    "qrcode": token,
                    "qrcode_img_content": f"https://qr.example/{token}",
                }
            )
        if "get_qrcode_status" in url:
            token = url.rsplit("qrcode=", 1)[-1]
            credentials = self.qr_by_token[token]
            return _Response(
                {
                    "status": "confirmed",
                    "ilink_bot_id": credentials.bot_id,
                    "bot_token": credentials.bot_token,
                    "baseurl": credentials.base_url,
                    "ilink_user_id": credentials.user_id,
                }
            )
        raise AssertionError(f"unexpected iLink GET endpoint: {url}")

    def post(self, url: str, *, data: str, **_kwargs):
        payload = json.loads(data)
        if "getupdates" in url:
            return _Response(self.update_batches.pop(0))
        if "sendmessage" in url:
            self.sent_messages.append(payload["msg"])
            if self.fail_next_send:
                self.fail_next_send = False
                return _Response({"error": "temporary"}, status=503)
            return _Response({"ret": 0})
        raise AssertionError(f"unexpected iLink POST endpoint: {url}")


@contextmanager
def _inference_server():
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            requests.append(request)
            reply = f"integration reply {len(requests)}"
            chunks = (
                {
                    "id": "ilink-integration",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": ""},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "ilink-integration",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": reply},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "ilink-integration",
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "stop"}
                    ],
                },
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def _wait_for_enrollment(manager: EnrollmentManager, attempt_id: str) -> None:
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        view = manager.get(attempt_id)
        if view is not None and view.status == "confirmed":
            return
        if view is not None and view.status in {"failed", "expired"}:
            raise AssertionError(f"enrollment ended as {view.status}")
        await asyncio.sleep(0.02)
    raise AssertionError("enrollment did not confirm")


async def _enroll(
    manager: EnrollmentManager,
    session: _FakeILinkSession,
    *,
    user_id: str,
) -> tuple[object, object]:
    session.qr_credentials.append(
        ILinkCredentials(
            bot_id=f"bot-{user_id}",
            bot_token=f"token-{user_id}",
            base_url="https://ilink.integration.test",
            user_id=user_id,
        )
    )
    attempt = await manager.create(
        source=f"source-{user_id}",
        device_id=f"device-{user_id}",
        scene="join",
    )
    await _wait_for_enrollment(manager, attempt.attempt_id)
    with manager.store.read() as conn:
        row = conn.execute(
            """
            SELECT b.binding_id FROM channel_bindings b
            JOIN external_identities e ON e.external_identity_id=b.external_identity_id
            WHERE e.subject_lookup_hash=?
            """,
            (manager.store.crypto.lookup_hash("external-subject", user_id),),
        ).fetchone()
    assert row is not None
    return resolve_binding(manager.store, binding_id=row["binding_id"])


@pytest.mark.asyncio
async def test_ilink_enrollment_poll_dispatch_send_and_generation_resume(monkeypatch):
    import hermes_cli.controlled_roots as controlled_roots
    import hermes_cli.owner_worker.supervisor as supervisor_module

    root = Path(tempfile.mkdtemp(prefix="hi", dir="/tmp"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_OWNER_SECRET", "ilink-integration-owner-secret")
    child_shim = root / "child-shim"
    child_shim.mkdir()
    (child_shim / "sitecustomize.py").write_text(
        "import hermes_cli.controlled_roots as controlled_roots\n"
        "controlled_roots.ControlledRoots._require_linux = lambda self: None\n"
        "controlled_roots._openat2 = lambda *args: None\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(child_shim))
    monkeypatch.setattr(controlled_roots.ControlledRoots, "_require_linux", lambda _self: None)
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)
    monkeypatch.setattr(
        supervisor_module,
        "_seed_owner_worker_skills",
        lambda _owner_home: {"copied": [], "updated": []},
    )

    store = ChannelIdentityStore(
        ChannelCrypto(
            lookup=Keyring(keys={1: b"l" * 32}, active_version=1),
            encryption=Keyring(keys={1: b"e" * 32}, active_version=1),
        )
    )
    ilink = _FakeILinkSession()
    enrollments = EnrollmentManager(store, ilink, poll_interval_seconds=0.01)
    supervisor = None
    try:
        with _inference_server() as (inference, model_requests):
            policy = DeploymentInferencePolicy(
                provider="custom:deployment",
                model="gpt-safe",
                api_mode="chat_completions",
                runtime_resolver=lambda: {
                    "provider": "custom:deployment",
                    "api_mode": "chat_completions",
                    "base_url": f"http://127.0.0.1:{inference.server_port}/v1",
                    "api_key": "control-plane-integration-secret",
                },
            )
            supervisor = OwnerWorkerSupervisor(
                control_home=root / "control-plane",
                global_home=root,
                startup_timeout=20,
                startup_cooldown=0,
                deployment_inference_policy=policy,
            )
            dispatcher = ChannelDispatcher(store, supervisor, turn_timeout=30)
            sender = OutboundSender(store, ilink, retry_seconds=0)

            owner_a, channel_a = await _enroll(
                enrollments,
                ilink,
                user_id="peer-a",
            )
            owner_b, channel_b = await _enroll(
                enrollments,
                ilink,
                user_id="peer-b",
            )
            assert owner_a.owner_key != owner_b.owner_key
            assert owner_a.owner_home != owner_b.owner_home
            assert owner_a.owner_home.is_dir()
            assert owner_b.owner_home.is_dir()

            for channel, peer, message_id, context in (
                (channel_a, "peer-a", "message-a-1", "context-a"),
                (channel_b, "peer-b", "message-b-1", "context-b"),
            ):
                lease = acquire_poll_lease(
                    store,
                    account_id=channel.account_id,
                    holder=f"poll-{peer}",
                )
                ilink.update_batches.append(
                    {
                        "msgs": [
                            {
                                "message_id": message_id,
                                "from_user_id": peer,
                                "context_token": context,
                                "item_list": [
                                    {"type": 1, "text_item": {"text": f"hello from {peer}"}}
                                ],
                            }
                        ],
                        "get_updates_buf": f"cursor-{peer}",
                    }
                )
                assert await AccountPoller(store, ilink, lease).poll_once(timeout_ms=1000) == 1

            for index in range(2):
                claim = dispatcher.claim_next(holder="integration-dispatch")
                assert claim is not None
                await dispatcher.dispatch_claim(claim, holder="integration-dispatch")
                outbound = claim_outbound(store, holder="integration-send")
                assert outbound is not None
                if index == 0:
                    ilink.fail_next_send = True
                    assert await sender.send_claim(outbound, holder="integration-send") is False
                    retry = claim_outbound(store, holder="integration-send")
                    assert retry is not None
                    assert retry.outbound_id == outbound.outbound_id
                    assert retry.client_message_id == outbound.client_message_id
                    assert await sender.send_claim(retry, holder="integration-send") is True
                else:
                    assert await sender.send_claim(outbound, holder="integration-send") is True

            assert len(model_requests) == 2
            assert {message["to_user_id"] for message in ilink.sent_messages} == {
                "peer-a",
                "peer-b",
            }
            assert [message["context_token"] for message in ilink.sent_messages[-2:]] == [
                "context-a",
                "context-b",
            ]
            assert (owner_a.owner_home / "state.db").is_file()
            assert (owner_b.owner_home / "state.db").is_file()
            with store.read() as conn:
                sessions = conn.execute(
                    "SELECT binding_id, owner_key, stored_session_id, worker_generation FROM channel_sessions"
                ).fetchall()
            assert len(sessions) == 2
            assert len({row["owner_key"] for row in sessions}) == 2
            assert len({row["stored_session_id"] for row in sessions}) == 2
            stored_a = next(
                row["stored_session_id"]
                for row in sessions
                if row["binding_id"] == channel_a.binding_id
            )

            lease_a = acquire_poll_lease(
                store,
                account_id=channel_a.account_id,
                holder="replay-poll-a",
            )
            replay = {
                "message_id": "message-a-1",
                "from_user_id": "peer-a",
                "context_token": "context-a",
                "item_list": [{"type": 1, "text_item": {"text": "hello from peer-a"}}],
            }
            assert commit_update_batch(
                store,
                lease_a,
                messages=(replay,),
                cursor="cursor-a-replay",
            ) == 0
            assert dispatcher.claim_next(holder="integration-dispatch") is None
            assert len(model_requests) == 2

            first_handle = supervisor.get_or_start(owner_a)
            first_generation = first_handle.worker_generation
            first_handle.process.terminate()
            first_handle.process.wait(timeout=5)
            await asyncio.sleep(0.1)

            assert commit_update_batch(
                store,
                lease_a,
                messages=(
                    {
                        "message_id": "message-a-2",
                        "from_user_id": "peer-a",
                        "context_token": "context-a-2",
                        "item_list": [
                            {"type": 1, "text_item": {"text": "resume after restart"}}
                        ],
                    },
                ),
                cursor="cursor-a-2",
            ) == 1
            resumed_claim = dispatcher.claim_next(holder="integration-dispatch")
            assert resumed_claim is not None
            await dispatcher.dispatch_claim(resumed_claim, holder="integration-dispatch")
            resumed_outbound = claim_outbound(store, holder="integration-send")
            assert resumed_outbound is not None
            assert await sender.send_claim(resumed_outbound, holder="integration-send") is True

            with store.read() as conn:
                resumed = conn.execute(
                    "SELECT stored_session_id, worker_generation FROM channel_sessions WHERE binding_id=?",
                    (channel_a.binding_id,),
                ).fetchone()
            active_lease = supervisor.authority_store.read_owner_worker_lease(owner_a.owner_key)
            assert active_lease is not None
            assert resumed["stored_session_id"] == stored_a
            assert resumed["worker_generation"] > first_generation
            assert resumed["worker_generation"] == active_lease.worker_generation
            assert len(model_requests) == 3
    finally:
        await enrollments.stop()
        if supervisor is not None:
            supervisor.shutdown()
        shutil.rmtree(root, ignore_errors=True)
