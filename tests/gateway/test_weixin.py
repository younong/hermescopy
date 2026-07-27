"""Tests for the Weixin platform adapter."""

import asyncio
import base64
import json
import os
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.config import GatewayConfig, HomeChannel, Platform, _apply_env_overrides
from gateway.platforms.base import SendResult
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms import weixin
from gateway.platforms.weixin import ContextTokenStore, WeixinAdapter
from tools.send_message_tool import _parse_target_ref, _send_to_platform


def _make_adapter() -> WeixinAdapter:
    return WeixinAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={"account_id": "test-account"},
        )
    )


class TestWeixinFormatting:
    def test_format_message_preserves_markdown(self):
        adapter = _make_adapter()

        content = "# Title\n\n## Plan\n\nUse **bold** and [docs](https://example.com)."

        assert adapter.format_message(content) == content

    def test_format_message_preserves_markdown_tables(self):
        adapter = _make_adapter()

        content = (
            "| Setting | Value |\n"
            "| --- | --- |\n"
            "| Timeout | 30s |\n"
            "| Retries | 3 |\n"
        )

        assert adapter.format_message(content) == content.strip()

    def test_format_message_preserves_fenced_code_blocks(self):
        adapter = _make_adapter()

        content = "## Snippet\n\n```python\nprint('hi')\n```"

        assert adapter.format_message(content) == content

    def test_format_message_wraps_long_plain_lines_for_copying(self):
        adapter = _make_adapter()

        content = (
            "Here is a long issue template line with many copyable fields "
            + " ".join(f"field_{idx}=value_{idx}" for idx in range(24))
        )

        formatted = adapter.format_message(content)

        assert "\n" in formatted
        assert all(len(line) <= weixin.WEIXIN_COPY_LINE_WIDTH for line in formatted.splitlines())
        assert " ".join(formatted.split()) == " ".join(content.split())

    def test_format_message_does_not_wrap_long_code_block_lines(self):
        adapter = _make_adapter()

        command = "hermes " + " ".join(f"--option-{idx}=value" for idx in range(30))
        content = f"```bash\n{command}\n```"

        assert adapter.format_message(content) == content

    def test_format_message_returns_empty_string_for_none(self):
        adapter = _make_adapter()

        assert adapter.format_message(None) == ""


class TestWeixinChunking:
    def test_split_text_splits_short_chatty_replies_into_separate_bubbles(self):
        adapter = _make_adapter()

        content = adapter.format_message("第一行\n第二行\n第三行")
        chunks = adapter._split_text(content)

        assert chunks == ["第一行", "第二行", "第三行"]

    def test_split_text_keeps_structured_table_block_together(self):
        adapter = _make_adapter()

        content = adapter.format_message(
            "- Setting: Timeout\n  Value: 30s\n- Setting: Retries\n  Value: 3"
        )
        chunks = adapter._split_text(content)

        assert chunks == ["- Setting: Timeout\n  Value: 30s\n- Setting: Retries\n  Value: 3"]

    def test_split_text_keeps_four_line_structured_blocks_together(self):
        adapter = _make_adapter()

        content = adapter.format_message(
            "今天结论：\n"
            "- 留存下降 3%\n"
            "- 转化上涨 8%\n"
            "- 主要问题在首日激活"
        )
        chunks = adapter._split_text(content)

        assert chunks == ["今天结论：\n- 留存下降 3%\n- 转化上涨 8%\n- 主要问题在首日激活"]

    def test_split_text_keeps_heading_with_body_together(self):
        adapter = _make_adapter()

        content = adapter.format_message("## 结论\n这是正文")
        chunks = adapter._split_text(content)

        assert chunks == ["## 结论\n这是正文"]

    def test_split_text_keeps_short_reformatted_table_in_single_chunk(self):
        adapter = _make_adapter()

        content = adapter.format_message(
            "| Setting | Value |\n"
            "| --- | --- |\n"
            "| Timeout | 30s |\n"
            "| Retries | 3 |\n"
        )
        chunks = adapter._split_text(content)

        assert chunks == [content]

    def test_split_text_keeps_complete_code_block_together_when_possible(self):
        adapter = _make_adapter()
        adapter.MAX_MESSAGE_LENGTH = 80

        content = adapter.format_message(
            "## Intro\n\nShort paragraph.\n\n```python\nprint('hello world')\nprint('again')\n```\n\nTail paragraph."
        )
        chunks = adapter._split_text(content)

        assert len(chunks) >= 2
        assert any(
            "```python\nprint('hello world')\nprint('again')\n```" in chunk
            for chunk in chunks
        )
        assert all(chunk.count("```") % 2 == 0 for chunk in chunks)

    def test_split_text_safely_splits_long_code_blocks(self):
        adapter = _make_adapter()
        adapter.MAX_MESSAGE_LENGTH = 70

        lines = "\n".join(f"line_{idx:02d} = {idx}" for idx in range(10))
        content = adapter.format_message(f"```python\n{lines}\n```")
        chunks = adapter._split_text(content)

        assert len(chunks) > 1
        assert all(len(chunk) <= adapter.MAX_MESSAGE_LENGTH for chunk in chunks)
        assert all(chunk.count("```") >= 2 for chunk in chunks)

    def test_split_text_can_restore_legacy_multiline_splitting_via_config(self):
        adapter = WeixinAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "account_id": "acct",
                    "token": "***",
                    "split_multiline_messages": True,
                },
            )
        )

        content = adapter.format_message("第一行\n第二行\n第三行")
        chunks = adapter._split_text(content)

        assert chunks == ["第一行", "第二行", "第三行"]


class TestWeixinConfig:
    def test_apply_env_overrides_configures_weixin(self):
        config = GatewayConfig()

        with patch.dict(
            os.environ,
            {
                "WEIXIN_ACCOUNT_ID": "bot-account",
                "WEIXIN_TOKEN": "bot-token",
                "WEIXIN_BASE_URL": "https://ilink.example.com/",
                "WEIXIN_CDN_BASE_URL": "https://cdn.example.com/c2c/",
                "WEIXIN_DM_POLICY": "allowlist",
                "WEIXIN_SPLIT_MULTILINE_MESSAGES": "true",
                "WEIXIN_ALLOWED_USERS": "wxid_1,wxid_2",
                "WEIXIN_HOME_CHANNEL": "wxid_1",
                "WEIXIN_HOME_CHANNEL_NAME": "Primary DM",
            },
            clear=True,
        ):
            _apply_env_overrides(config)

        platform_config = config.platforms[Platform.WEIXIN]
        assert platform_config.enabled is True
        assert platform_config.token == "bot-token"
        assert platform_config.extra["account_id"] == "bot-account"
        assert platform_config.extra["base_url"] == "https://ilink.example.com"
        assert platform_config.extra["cdn_base_url"] == "https://cdn.example.com/c2c"
        assert platform_config.extra["dm_policy"] == "allowlist"
        assert platform_config.extra["split_multiline_messages"] == "true"
        assert platform_config.extra["allow_from"] == "wxid_1,wxid_2"
        assert platform_config.home_channel == HomeChannel(Platform.WEIXIN, "wxid_1", "Primary DM")

    def test_get_connected_platforms_includes_weixin_with_token(self):
        config = GatewayConfig(
            platforms={
                Platform.WEIXIN: PlatformConfig(
                    enabled=True,
                    token="bot-token",
                    extra={"account_id": "bot-account"},
                )
            }
        )

        assert config.get_connected_platforms() == [Platform.WEIXIN]

    def test_get_connected_platforms_requires_account_id(self):
        config = GatewayConfig(
            platforms={
                Platform.WEIXIN: PlatformConfig(
                    enabled=True,
                    token="bot-token",
                )
            }
        )

        assert config.get_connected_platforms() == []


class TestWeixinStatePersistence:
    def test_save_weixin_account_preserves_existing_file_on_replace_failure(self, tmp_path, monkeypatch):
        account_path = tmp_path / "weixin" / "accounts" / "acct.json"
        account_path.parent.mkdir(parents=True, exist_ok=True)
        original = {"token": "old-token", "base_url": "https://old.example.com"}
        account_path.write_text(json.dumps(original), encoding="utf-8")

        def _boom(_src, _dst):
            raise OSError("disk full")

        monkeypatch.setattr("utils.os.replace", _boom)

        try:
            weixin.save_weixin_account(
                str(tmp_path),
                account_id="acct",
                token="new-token",
                base_url="https://new.example.com",
                user_id="wxid_new",
            )
        except OSError:
            pass
        else:
            raise AssertionError("expected save_weixin_account to propagate replace failure")

        assert json.loads(account_path.read_text(encoding="utf-8")) == original

    def test_context_token_persist_preserves_existing_file_on_replace_failure(self, tmp_path, monkeypatch):
        token_path = tmp_path / "weixin" / "accounts" / "acct.context-tokens.json"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(json.dumps({"user-a": "old-token"}), encoding="utf-8")

        def _boom(_src, _dst):
            raise OSError("disk full")

        monkeypatch.setattr("utils.os.replace", _boom)

        store = ContextTokenStore(str(tmp_path))
        with patch.object(weixin.logger, "warning") as warning_mock:
            store.set("acct", "user-b", "new-token")

        assert json.loads(token_path.read_text(encoding="utf-8")) == {"user-a": "old-token"}
        warning_mock.assert_called_once()

    def test_save_sync_buf_preserves_existing_file_on_replace_failure(self, tmp_path, monkeypatch):
        sync_path = tmp_path / "weixin" / "accounts" / "acct.sync.json"
        sync_path.parent.mkdir(parents=True, exist_ok=True)
        sync_path.write_text(json.dumps({"get_updates_buf": "old-sync"}), encoding="utf-8")

        def _boom(_src, _dst):
            raise OSError("disk full")

        monkeypatch.setattr("utils.os.replace", _boom)

        try:
            weixin._save_sync_buf(str(tmp_path), "acct", "new-sync")
        except OSError:
            pass
        else:
            raise AssertionError("expected _save_sync_buf to propagate replace failure")

        assert json.loads(sync_path.read_text(encoding="utf-8")) == {"get_updates_buf": "old-sync"}


class TestWeixinQrLogin:
    @pytest.mark.asyncio
    async def test_qr_login_timeout_uses_monotonic_clock(self, tmp_path):
        first_qr = {
            "qrcode": "qr-1",
            "qrcode_img_content": "https://example.com/qr-1",
        }
        pending = {"status": "wait"}

        with patch("gateway.platforms.weixin._api_get", new_callable=AsyncMock) as api_get_mock, \
             patch("gateway.platforms.weixin.time") as mock_time, \
             patch("gateway.platforms.weixin.AIOHTTP_AVAILABLE", True), \
             patch("gateway.platforms.weixin.aiohttp.ClientSession", create=True) as session_cls, \
             patch("builtins.print"):
            api_get_mock.side_effect = [first_qr, pending]
            mock_time.monotonic.side_effect = [1000, 1000.2, 1001.1]
            mock_time.time.side_effect = [1000, 900, 901, 902]

            session = AsyncMock()
            session.__aenter__.return_value = session
            session.__aexit__.return_value = False
            session_cls.return_value = session

            result = await weixin.qr_login(str(tmp_path), timeout_seconds=1)

        assert result is None
        assert api_get_mock.await_count == 2


class TestWeixinSendMessageIntegration:
    def test_parse_target_ref_accepts_weixin_ids(self):
        assert _parse_target_ref("weixin", "wxid_test123") == ("wxid_test123", None, True)
        assert _parse_target_ref("weixin", "filehelper") == ("filehelper", None, True)
        assert _parse_target_ref("weixin", "group@chatroom") == ("group@chatroom", None, True)

    @patch("tools.send_message_tool._send_weixin", new_callable=AsyncMock)
    def test_send_to_platform_routes_weixin_media_to_native_helper(self, send_weixin_mock):
        send_weixin_mock.return_value = {"success": True, "platform": "weixin", "chat_id": "wxid_test123"}
        config = PlatformConfig(enabled=True, token="bot-token", extra={"account_id": "bot-account"})

        result = asyncio.run(
            _send_to_platform(
                Platform.WEIXIN,
                config,
                "wxid_test123",
                "hello",
                media_files=[("/tmp/demo.png", False)],
            )
        )

        assert result["success"] is True
        send_weixin_mock.assert_awaited_once_with(
            config,
            "wxid_test123",
            "hello",
            media_files=[("/tmp/demo.png", False)],
        )


class TestWeixinChunkDelivery:
    def _connected_adapter(self) -> WeixinAdapter:
        adapter = _make_adapter()
        adapter._session = object()
        adapter._send_session = adapter._session
        adapter._token = "test-token"
        adapter._base_url = "https://weixin.example.com"
        adapter._token_store.get = lambda account_id, chat_id: "ctx-token"
        return adapter

    @patch("gateway.platforms.weixin.asyncio.sleep", new_callable=AsyncMock)
    @patch("gateway.platforms.weixin._send_message", new_callable=AsyncMock)
    def test_send_waits_between_multiple_chunks(self, send_message_mock, sleep_mock):
        adapter = self._connected_adapter()
        adapter.MAX_MESSAGE_LENGTH = 12

        # Use double newlines so _pack_markdown_blocks splits into 3 blocks
        result = asyncio.run(adapter.send("wxid_test123", "first\n\nsecond\n\nthird"))

        assert result.success is True
        assert send_message_mock.await_count == 3
        assert sleep_mock.await_count == 2

    @patch("gateway.platforms.weixin.asyncio.sleep", new_callable=AsyncMock)
    @patch("gateway.platforms.weixin._send_message", new_callable=AsyncMock)
    def test_send_retries_failed_chunk_before_continuing(self, send_message_mock, sleep_mock):
        adapter = self._connected_adapter()
        adapter.MAX_MESSAGE_LENGTH = 12
        calls = {"count": 0}

        async def flaky_send(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("temporary iLink failure")

        send_message_mock.side_effect = flaky_send

        # Use double newlines so _pack_markdown_blocks splits into 3 blocks
        result = asyncio.run(adapter.send("wxid_test123", "first\n\nsecond\n\nthird"))

        assert result.success is True
        # 3 chunks, but chunk 2 fails once and retries → 4 _send_message calls total
        assert send_message_mock.await_count == 4
        # The retried chunk should reuse the same client_id for deduplication
        first_try = send_message_mock.await_args_list[1].kwargs
        retry = send_message_mock.await_args_list[2].kwargs
        assert first_try["text"] == retry["text"]
        assert first_try["client_id"] == retry["client_id"]

    @patch("gateway.platforms.weixin.asyncio.sleep", new_callable=AsyncMock)
    @patch("gateway.platforms.weixin._send_message", new_callable=AsyncMock)
    def test_repeated_rate_limits_open_circuit_for_followup_sends(self, send_message_mock, sleep_mock):
        adapter = self._connected_adapter()
        adapter._send_chunk_retries = 3
        adapter._send_chunk_retry_delay_seconds = 0
        adapter._rate_limit_circuit_threshold = 2
        adapter._rate_limit_circuit_window_seconds = 60
        adapter._rate_limit_circuit_open_seconds = 60

        send_message_mock.return_value = {
            "ret": weixin.RATE_LIMIT_ERRCODE,
            "errcode": weixin.RATE_LIMIT_ERRCODE,
            "errmsg": "frequency limit",
        }

        first = asyncio.run(adapter.send("wxid_test123", "first"))
        second = asyncio.run(adapter.send("wxid_test123", "second"))

        assert first.success is False
        assert "cooldown" in (first.error or "")
        assert second.success is False
        assert "cooldown" in (second.error or "")
        # The first rate-limit response is retried once. The second response
        # crosses the sliding-window threshold, opens the breaker, and both the
        # rest of the current chunk and follow-up sends fail fast.
        assert send_message_mock.await_count == 2
        assert sleep_mock.await_count == 1

    @patch("gateway.platforms.weixin._send_message", new_callable=AsyncMock)
    def test_open_rate_limit_circuit_fails_fast_without_sendmessage(self, send_message_mock):
        adapter = self._connected_adapter()
        adapter._rate_limit_circuit_open_seconds = 60
        adapter._open_rate_limit_circuit()

        result = asyncio.run(adapter.send("wxid_test123", "blocked"))

        assert result.success is False
        assert "cooldown" in (result.error or "")
        send_message_mock.assert_not_awaited()

    @patch("gateway.platforms.weixin._send_message", new_callable=AsyncMock)
    def test_successful_send_after_cooldown_resets_rate_limit_state(self, send_message_mock):
        adapter = self._connected_adapter()
        adapter._rate_limit_circuit_until = weixin.time.monotonic() - 1
        adapter._rate_limit_events = [weixin.time.monotonic()]
        send_message_mock.return_value = {"errcode": 0}

        result = asyncio.run(adapter.send("wxid_test123", "after cooldown"))

        assert result.success is True
        assert adapter._rate_limit_events == []
        assert adapter._rate_limit_circuit_until == 0.0
        send_message_mock.assert_awaited_once()

    def test_concurrent_rate_limited_sends_are_serialized_by_gate(self):
        adapter = self._connected_adapter()
        adapter._send_chunk_retries = 3
        adapter._send_chunk_retry_delay_seconds = 0
        adapter._rate_limit_circuit_threshold = 1
        adapter._rate_limit_circuit_open_seconds = 60
        active = 0
        peak_active = 0

        async def rate_limited_send(*args, **kwargs):
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            await asyncio.sleep(0)
            active -= 1
            return {
                "ret": weixin.RATE_LIMIT_ERRCODE,
                "errcode": weixin.RATE_LIMIT_ERRCODE,
                "errmsg": "frequency limit",
            }

        async def run_burst():
            with patch("gateway.platforms.weixin._send_message", side_effect=rate_limited_send) as send_message_mock:
                results = await asyncio.gather(
                    *(adapter.send("wxid_test123", f"message {idx}") for idx in range(20))
                )
                return results, send_message_mock

        results, send_message_mock = asyncio.run(run_burst())

        assert all(not result.success for result in results)
        assert peak_active == 1
        # Once the first send observes iLink's rate limit, the breaker opens;
        # queued concurrent sends acquire the gate later and fail before making
        # their own iLink calls.
        assert send_message_mock.await_count == 1


class TestWeixinOutboundMedia:
    def test_send_image_file_accepts_keyword_image_path(self):
        adapter = _make_adapter()
        expected = SendResult(success=True, message_id="msg-1")
        adapter.send_document = AsyncMock(return_value=expected)

        result = asyncio.run(
            adapter.send_image_file(
                chat_id="wxid_test123",
                image_path="/tmp/demo.png",
                caption="截图说明",
                reply_to="reply-1",
                metadata={"thread_id": "t-1"},
            )
        )

        assert result == expected
        adapter.send_document.assert_awaited_once_with(
            chat_id="wxid_test123",
            file_path="/tmp/demo.png",
            caption="截图说明",
            metadata={"thread_id": "t-1"},
        )

    def test_send_document_accepts_keyword_file_path(self):
        adapter = _make_adapter()
        adapter._session = object()
        adapter._send_session = adapter._session
        adapter._token = "test-token"
        adapter._send_file = AsyncMock(return_value="msg-2")

        result = asyncio.run(
            adapter.send_document(
                chat_id="wxid_test123",
                file_path="/tmp/report.pdf",
                caption="报告请看",
                file_name="renamed.pdf",
                reply_to="reply-1",
                metadata={"thread_id": "t-1"},
            )
        )

        assert result.success is True
        assert result.message_id == "msg-2"
        adapter._send_file.assert_awaited_once_with("wxid_test123", "/tmp/report.pdf", "报告请看")

    def test_send_file_uses_shared_upload_and_item_sender(self, tmp_path):
        image_path = tmp_path / "demo.png"
        image_path.write_bytes(b"fake-png-bytes")

        adapter = _make_adapter()
        adapter._session = object()
        adapter._send_session = adapter._session
        adapter._token = "test-token"
        adapter._base_url = "https://weixin.example.com"
        adapter._cdn_base_url = "https://novac2c.cdn.weixin.qq.com/c2c"
        adapter._token_store.get = lambda account_id, chat_id: "context"
        item = {"type": weixin.ITEM_IMAGE, "image_item": {"media": {}}}

        with patch(
            "gateway.platforms.weixin.upload_media_item",
            new=AsyncMock(return_value=item),
        ) as upload_mock, patch.object(
            weixin.WeixinILinkClient,
            "send_item",
            new=AsyncMock(return_value={"ret": 0}),
        ) as send_item_mock:
            message_id = asyncio.run(adapter._send_file("wxid_test123", str(image_path), ""))

        assert message_id.startswith("hermes-weixin-")
        upload_mock.assert_awaited_once()
        assert upload_mock.await_args.kwargs["path"] == str(image_path)
        assert upload_mock.await_args.kwargs["force_file"] is False
        send_item_mock.assert_awaited_once_with(
            to="wxid_test123",
            item=item,
            context_token="context",
            client_id=message_id,
            raise_provider_errors=False,
        )


class TestWeixinRemoteMediaSafety:
    def test_download_remote_media_blocks_unsafe_urls(self):
        adapter = _make_adapter()

        with patch("tools.url_safety.is_safe_url", return_value=False):
            try:
                asyncio.run(adapter._download_remote_media("http://127.0.0.1/private.png"))
            except ValueError as exc:
                assert "Blocked unsafe URL" in str(exc)
            else:
                raise AssertionError("expected ValueError for unsafe URL")


class TestWeixinMarkdownLinks:
    """Markdown links should be preserved so WeChat can render them natively."""

    def test_format_message_preserves_markdown_links(self):
        adapter = _make_adapter()

        content = "Check [the docs](https://example.com) and [GitHub](https://github.com) for details"
        assert adapter.format_message(content) == content

    def test_format_message_preserves_links_inside_code_blocks(self):
        adapter = _make_adapter()

        content = "See below:\n\n```\n[link](https://example.com)\n```\n\nDone."
        result = adapter.format_message(content)
        assert "[link](https://example.com)" in result


class TestWeixinBlankMessagePrevention:
    """Regression tests for the blank-bubble bugs.

    Three separate guards now prevent a blank WeChat message from ever being
    dispatched:

    1. ``_split_text_for_weixin_delivery("")`` returns ``[]`` — not ``[""]``.
    2. ``send()`` filters out empty/whitespace-only chunks before calling
       ``_send_text_chunk``.
    3. ``_send_message()`` raises ``ValueError`` for empty text as a last-resort
       safety net.
    """

    def test_split_text_returns_empty_list_for_empty_string(self):
        adapter = _make_adapter()
        assert adapter._split_text("") == []

    def test_split_text_returns_empty_list_for_empty_string_split_per_line(self):
        adapter = WeixinAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "account_id": "acct",
                    "token": "test-tok",
                    "split_multiline_messages": True,
                },
            )
        )
        assert adapter._split_text("") == []

    @patch("gateway.platforms.weixin._send_message", new_callable=AsyncMock)
    def test_send_empty_content_does_not_call_send_message(self, send_message_mock):
        adapter = _make_adapter()
        adapter._session = object()
        adapter._send_session = adapter._session
        adapter._token = "test-token"
        adapter._base_url = "https://weixin.example.com"
        adapter._token_store.get = lambda account_id, chat_id: "ctx-token"

        result = asyncio.run(adapter.send("wxid_test123", ""))
        # Empty content → no chunks → no _send_message calls
        assert result.success is True
        send_message_mock.assert_not_awaited()

    def test_send_message_rejects_empty_text(self):
        """_send_message raises ValueError for empty/whitespace text."""
        import pytest
        with pytest.raises(ValueError, match="text must not be empty"):
            asyncio.run(
                weixin._send_message(
                    AsyncMock(),
                    base_url="https://example.com",
                    token="tok",
                    to="wxid_test",
                    text="",
                    context_token=None,
                    client_id="cid",
                )
            )


class TestWeixinInboundMediaTypes:
    def test_native_voice_remains_voice(self):
        assert weixin._message_type_from_media(
            ["audio/silk"], "", native_voice=True
        ) == weixin.MessageType.VOICE

    def test_audio_file_is_audio(self):
        assert weixin._message_type_from_media(
            ["audio/mpeg"], "", native_voice=False
        ) == weixin.MessageType.AUDIO


class TestWeixinStreamingCursorSuppression:
    """WeChat doesn't support message editing — cursor must be suppressed."""

    def test_supports_message_editing_is_false(self):
        adapter = _make_adapter()
        assert adapter.SUPPORTS_MESSAGE_EDITING is False


class TestWeixinSendImageFileParameterName:
    """Regression test for send_image_file parameter name mismatch.

    The gateway calls send_image_file(chat_id=..., image_path=...) but the
    WeixinAdapter previously used 'path' as the parameter name, causing
    image sending to fail. This test ensures the interface stays correct.
    """

    @patch.object(WeixinAdapter, "send_document", new_callable=AsyncMock)
    def test_send_image_file_uses_image_path_parameter(self, send_document_mock):
        """Verify send_image_file accepts image_path and forwards to send_document."""
        adapter = _make_adapter()
        adapter._session = object()
        adapter._send_session = adapter._session
        adapter._token = "test-token"

        send_document_mock.return_value = weixin.SendResult(success=True, message_id="test-id")

        # This is the call pattern used by gateway/run.py extract_media
        result = asyncio.run(
            adapter.send_image_file(
                chat_id="wxid_test123",
                image_path="/tmp/test_image.png",
                caption="Test caption",
                metadata={"thread_id": "thread-123"},
            )
        )

        assert result.success is True
        send_document_mock.assert_awaited_once_with(
            chat_id="wxid_test123",
            file_path="/tmp/test_image.png",
            caption="Test caption",
            metadata={"thread_id": "thread-123"},
        )

    @patch.object(WeixinAdapter, "send_document", new_callable=AsyncMock)
    def test_send_image_file_works_without_optional_params(self, send_document_mock):
        """Verify send_image_file works with minimal required params."""
        adapter = _make_adapter()
        adapter._session = object()
        adapter._send_session = adapter._session
        adapter._token = "test-token"

        send_document_mock.return_value = weixin.SendResult(success=True, message_id="test-id")

        result = asyncio.run(
            adapter.send_image_file(
                chat_id="wxid_test123",
                image_path="/tmp/test_image.jpg",
            )
        )

        assert result.success is True
        send_document_mock.assert_awaited_once_with(
            chat_id="wxid_test123",
            file_path="/tmp/test_image.jpg",
            caption=None,
            metadata=None,
        )


class TestWeixinVoiceSending:
    def _connected_adapter(self) -> WeixinAdapter:
        adapter = _make_adapter()
        adapter._session = object()
        adapter._send_session = adapter._session
        adapter._token = "test-token"
        adapter._base_url = "https://weixin.example.com"
        adapter._token_store.get = lambda account_id, chat_id: "ctx-token"
        return adapter

    @patch.object(WeixinAdapter, "_send_file", new_callable=AsyncMock)
    def test_send_voice_downgrades_to_document_attachment(self, send_file_mock, tmp_path):
        adapter = self._connected_adapter()
        source = tmp_path / "voice.ogg"
        source.write_bytes(b"ogg")
        send_file_mock.return_value = "msg-1"

        result = asyncio.run(adapter.send_voice("wxid_test123", str(source)))

        assert result.success is True
        send_file_mock.assert_awaited_once_with(
            "wxid_test123",
            str(source),
            "[voice message as attachment]",
            force_file_attachment=True,
        )

    def test_send_file_uses_native_voice_item_for_silk_payload(self, tmp_path):
        adapter = self._connected_adapter()
        silk = tmp_path / "voice.silk"
        silk.write_bytes(b"\x02#!SILK_V3\x01\x00")
        item = {
            "type": weixin.ITEM_VOICE,
            "voice_item": {
                "playtime": 0,
                "encode_type": 6,
                "sample_rate": 24000,
                "bits_per_sample": 16,
            },
        }

        with patch(
            "gateway.platforms.weixin.upload_media_item",
            new=AsyncMock(return_value=item),
        ) as upload_mock, patch.object(
            weixin.WeixinILinkClient,
            "send_item",
            new=AsyncMock(return_value={"ret": 0}),
        ) as send_item_mock:
            asyncio.run(adapter._send_file("wxid_test123", str(silk), ""))

        assert upload_mock.await_args.kwargs["force_file"] is False
        assert send_item_mock.await_args.kwargs["item"] == item


class TestIsStaleSessionRet:
    """Regression test for #17228: distinguish stale-session ret=-2 from rate-limit ret=-2."""

    def test_ret_minus_2_with_unknown_error_is_stale(self):
        assert weixin._is_stale_session_ret(-2, None, "unknown error") is True

    def test_errcode_minus_2_with_unknown_error_is_stale(self):
        assert weixin._is_stale_session_ret(None, -2, "unknown error") is True

    def test_unknown_error_case_insensitive(self):
        assert weixin._is_stale_session_ret(-2, None, "Unknown Error") is True

    def test_ret_minus_2_with_freq_limit_is_not_stale(self):
        # Genuine rate limit — must NOT be treated as stale session.
        assert weixin._is_stale_session_ret(-2, None, "freq limit") is False

    def test_ret_minus_2_with_no_errmsg_is_not_stale(self):
        assert weixin._is_stale_session_ret(-2, None, None) is False
        assert weixin._is_stale_session_ret(-2, None, "") is False

    def test_errcode_minus_14_is_not_matched_here(self):
        # -14 is handled by the separate SESSION_EXPIRED_ERRCODE path; the
        # helper only disambiguates -2 from a genuine rate limit.
        assert weixin._is_stale_session_ret(-14, None, "session expired") is False

    def test_success_codes_are_not_stale(self):
        assert weixin._is_stale_session_ret(0, 0, "") is False
        assert weixin._is_stale_session_ret(None, None, "unknown error") is False


class TestWeixinContentDedup:
    """Regression tests for Issue #16182 — upstream API sends duplicate content
    with different message_ids, bypassing message_id deduplication.
    """

    def test_duplicate_content_with_different_message_ids_is_dropped(self):
        adapter = _make_adapter()
        adapter._poll_session = object()
        adapter.handle_message = AsyncMock()
        # Tighten the text-debounce delay so the flush completes quickly.
        adapter._text_batch_delay_seconds = 0.05
        adapter._text_batch_split_delay_seconds = 0.05

        base_msg = {
            "from_user_id": "wxid_user1",
            "item_list": [{"type": 1, "text_item": {"text": "hello world"}}],
        }

        async def _drive():
            # Both inbound messages share the same event loop so the debounce
            # task created by the first one survives to be flushed.
            await adapter._process_message({**base_msg, "message_id": "msg-1"})
            await adapter._process_message({**base_msg, "message_id": "msg-2"})
            # Wait out the quiet period so the buffered text batch flushes.
            await asyncio.sleep(0.2)

        asyncio.run(_drive())

        # Content-dedup drops the second (duplicate) message before it is even
        # enqueued, so only one combined dispatch reaches handle_message.
        assert adapter.handle_message.await_count == 1
        event = adapter.handle_message.await_args[0][0]
        assert event.text == "hello world"

    def test_content_dedup_not_called_for_messages_without_text(self):
        adapter = _make_adapter()
        adapter._poll_session = object()
        adapter.handle_message = AsyncMock()
        adapter._dedup.is_duplicate = Mock(return_value=False)

        empty_msg = {
            "from_user_id": "wxid_user1",
            "message_id": "msg-1",
            "item_list": [],
        }
        asyncio.run(adapter._process_message(empty_msg))

        assert adapter.handle_message.await_count == 0
        # is_duplicate should only be called for message_id, never for content
        assert all("content:" not in str(call) for call in adapter._dedup.is_duplicate.call_args_list)


class TestWeixinTextDebounce:
    """Text-debounce batching for rapid multi-message bursts (issue #35301).

    Delays are read from ``config.extra`` (config.yaml), not env vars.
    """

    def test_batch_delays_default_from_config(self):
        adapter = _make_adapter()
        assert adapter._text_batch_delay_seconds == 3.0
        assert adapter._text_batch_split_delay_seconds == 5.0

    def test_batch_delays_overridden_via_config_extra(self):
        adapter = WeixinAdapter(
            PlatformConfig(
                enabled=True,
                token="test-token",
                extra={
                    "account_id": "test-account",
                    "text_batch_delay_seconds": "0.5",
                    "text_batch_split_delay_seconds": 1.5,
                },
            )
        )
        assert adapter._text_batch_delay_seconds == 0.5
        assert adapter._text_batch_split_delay_seconds == 1.5

    def test_invalid_config_value_falls_back_to_default(self):
        adapter = WeixinAdapter(
            PlatformConfig(
                enabled=True,
                token="test-token",
                extra={
                    "account_id": "test-account",
                    "text_batch_delay_seconds": "not-a-number",
                    "text_batch_split_delay_seconds": -4,
                },
            )
        )
        assert adapter._text_batch_delay_seconds == 3.0
        assert adapter._text_batch_split_delay_seconds == 5.0

    def test_rapid_texts_collapse_into_single_dispatch(self):
        adapter = _make_adapter()
        adapter._text_batch_delay_seconds = 0.05
        adapter._text_batch_split_delay_seconds = 0.05
        dispatched = []

        async def _capture(event):
            dispatched.append(event.text)

        adapter.handle_message = _capture

        def _event(text):
            return MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=adapter.build_source(
                    chat_id="wxid_user1", chat_type="dm",
                    user_id="wxid_user1", user_name="wxid_user1",
                ),
            )

        async def _drive():
            adapter._enqueue_text_event(_event("one"))
            adapter._enqueue_text_event(_event("two"))
            adapter._enqueue_text_event(_event("three"))
            assert dispatched == []  # nothing flushed during the burst
            await asyncio.sleep(0.2)

        asyncio.run(_drive())
        assert dispatched == ["one\ntwo\nthree"]


class _StubResponse:
    def __init__(self, *, status=200, body="{}", delay=0.0):
        self.status = status
        self.ok = 200 <= status < 300
        self._body = body
        self._delay = delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def text(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._body


class _StubSession:
    """Records request kwargs and returns a configurable async-CM response.

    Unlike aiohttp.ClientSession it installs no TimerContext, so it cannot
    reproduce aiohttp's cross-loop crash directly; these tests instead pin the
    observable contract of the asyncio.wait_for migration.
    """

    def __init__(self, response):
        self._response = response
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._response

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._response


class TestWeixinApiTimeout:
    def test_api_post_does_not_pass_aiohttp_timeout_kwarg(self):
        session = _StubSession(_StubResponse(body='{"ret": 0}'))
        result = asyncio.run(
            weixin._api_post(
                session,
                base_url="https://weixin.example.com",
                endpoint="ep",
                payload={"k": "v"},
                token="tok",
                timeout_ms=5000,
            )
        )
        assert result == {"ret": 0}
        # The fix enforces the timeout via asyncio.wait_for, so ClientTimeout is
        # gone and `timeout` is no longer forwarded to session.post().
        [(_url, kwargs)] = session.post_calls
        assert "timeout" not in kwargs

    def test_api_get_does_not_pass_aiohttp_timeout_kwarg(self):
        session = _StubSession(_StubResponse(body='{"ret": 0}'))
        result = asyncio.run(
            weixin._api_get(
                session,
                base_url="https://weixin.example.com",
                endpoint="ep",
                timeout_ms=5000,
            )
        )
        assert result == {"ret": 0}
        [(_url, kwargs)] = session.get_calls
        assert "timeout" not in kwargs

    def test_api_post_raises_timeout_when_response_is_slow(self):
        # 1 ms budget against a 1 s response: wait_for must cancel and raise.
        session = _StubSession(_StubResponse(delay=1.0))
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(
                weixin._api_post(
                    session,
                    base_url="https://weixin.example.com",
                    endpoint="ep",
                    payload={"k": "v"},
                    token="tok",
                    timeout_ms=1,
                )
            )

    def test_api_get_raises_timeout_when_response_is_slow(self):
        session = _StubSession(_StubResponse(delay=1.0))
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(
                weixin._api_get(
                    session,
                    base_url="https://weixin.example.com",
                    endpoint="ep",
                    timeout_ms=1,
                )
            )

    def test_api_post_raises_sanitized_runtime_error_on_non_ok_status(self):
        session = _StubSession(_StubResponse(status=500, body="provider-secret"))
        with pytest.raises(RuntimeError, match="iLink POST ep HTTP 500: request failed") as caught:
            asyncio.run(
                weixin._api_post(
                    session,
                    base_url="https://weixin.example.com",
                    endpoint="ep",
                    payload={"k": "v"},
                    token="tok",
                    timeout_ms=5000,
                )
            )
        assert "provider-secret" not in str(caught.value)

    def test_api_get_raises_sanitized_runtime_error_on_non_ok_status(self):
        session = _StubSession(_StubResponse(status=500, body="provider-secret"))
        with pytest.raises(RuntimeError, match="iLink GET ep HTTP 500: request failed") as caught:
            asyncio.run(
                weixin._api_get(
                    session,
                    base_url="https://weixin.example.com",
                    endpoint="ep",
                    timeout_ms=5000,
                )
            )
        assert "provider-secret" not in str(caught.value)

    def test_get_updates_returns_empty_sentinel_on_timeout(self):
        # wait_for raises asyncio.TimeoutError, which _get_updates swallows into
        # an empty long-poll batch rather than propagating.
        session = _StubSession(_StubResponse(delay=1.0))
        result = asyncio.run(
            weixin._get_updates(
                session,
                base_url="https://weixin.example.com",
                token="tok",
                sync_buf="buf-123",
                timeout_ms=1,
            )
        )
        assert result == {"ret": 0, "msgs": [], "get_updates_buf": "buf-123"}
