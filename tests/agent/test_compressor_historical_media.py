"""Tests for post-compression historical-media stripping.

Port of Kilo-Org/kilocode#9434 (adapted for OpenAI-style message lists).
Without this pass, tail messages keep their original multi-MB base-64 image
payloads after context compression, and every subsequent request re-ships
them — sometimes breaching provider body-size limits and wedging the
session.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from agent.message_sanitization import (
    project_attachment_content,
    project_historical_attachments,
)


IMG_URL = {
    "type": "image_url",
    "image_url": {"url": "data:image/png;base64," + ("A" * 1024)},
}
INPUT_IMG = {
    "type": "input_image",
    "image_url": "data:image/png;base64," + ("B" * 1024),
}
ANTHROPIC_IMG = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "C" * 1024},
}
TEXT = {"type": "text", "text": "hi"}
INPUT_TEXT = {"type": "input_text", "text": "hi"}


def is_image_part(part):
    return isinstance(part, dict) and part.get("type") in {"image", "image_url", "input_image"}


def content_has_images(content):
    return isinstance(content, list) and any(is_image_part(part) for part in content)


class TestIsImagePart:
    def test_openai_chat_shape(self):
        assert is_image_part(IMG_URL) is True

    def test_openai_responses_shape(self):
        assert is_image_part(INPUT_IMG) is True

    def test_anthropic_native_shape(self):
        assert is_image_part(ANTHROPIC_IMG) is True

    def test_text_part_is_not_image(self):
        assert is_image_part(TEXT) is False
        assert is_image_part(INPUT_TEXT) is False

    def test_non_dict_rejected(self):
        assert is_image_part("image") is False
        assert is_image_part(None) is False
        assert is_image_part(42) is False


class TestContentHasImages:
    def test_string_content(self):
        assert content_has_images("a string") is False

    def test_empty_list(self):
        assert content_has_images([]) is False

    def test_text_only_list(self):
        assert content_has_images([TEXT, TEXT]) is False

    def test_list_with_image(self):
        assert content_has_images([TEXT, IMG_URL]) is True

    def test_none(self):
        assert content_has_images(None) is False


class TestStripImagesFromContent:
    def test_string_passthrough(self):
        assert project_attachment_content("hello") == "hello"

    def test_none_passthrough(self):
        assert project_attachment_content(None) is None

    def test_text_only_passthrough(self):
        parts = [TEXT, {"type": "text", "text": "world"}]
        assert project_attachment_content(parts) == parts

    def test_replaces_image_with_placeholder(self):
        parts = [TEXT, IMG_URL]
        out = project_attachment_content(parts)
        assert len(out) == 2
        assert out[0] == TEXT
        assert out[1] == {
            "type": "text",
            "text": (
                "[Attached image — payload omitted; explicitly attach it again to reuse]"
            ),
        }

    def test_does_not_mutate_input(self):
        parts = [IMG_URL, TEXT]
        _ = project_attachment_content(parts)
        assert parts[0] is IMG_URL  # original list untouched
        assert parts[1] is TEXT

    def test_handles_all_three_shapes(self):
        parts = [IMG_URL, INPUT_IMG, ANTHROPIC_IMG, TEXT]
        out = project_attachment_content(parts)
        assert sum(1 for p in out if p.get("type") == "text") == 4
        assert not any(is_image_part(p) for p in out)


class TestStripHistoricalMedia:
    def test_empty_passthrough(self):
        assert project_historical_attachments([]) == []

    def test_no_images_anywhere(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey"},
            {"role": "user", "content": "bye"},
        ]
        assert project_historical_attachments(msgs) is msgs  # identity — no copy

    def test_single_image_user_is_projected(self):
        msgs = [
            {"role": "user", "content": [TEXT, IMG_URL]},
            {"role": "assistant", "content": "ok"},
        ]
        out = project_historical_attachments(msgs)
        assert out is not msgs
        assert not content_has_images(out[0]["content"])
        assert "payload omitted" in str(out[0]["content"])

    def test_strips_older_user_image_keeps_newest(self):
        msgs = [
            {"role": "user", "content": [TEXT, IMG_URL]},     # old — strip
            {"role": "assistant", "content": "looked at it"},
            {"role": "user", "content": [TEXT, INPUT_IMG]},   # newest — keep
        ]
        out = project_historical_attachments(msgs)
        assert out is not msgs  # new list
        # First message's image was replaced
        assert not content_has_images(out[0]["content"])
        # The universal projection no longer preserves a newest-image anchor.
        assert not content_has_images(out[2]["content"])

    def test_strips_assistant_and_tool_images_before_anchor(self):
        msgs = [
            {"role": "user", "content": [TEXT, IMG_URL]},          # old user
            {"role": "assistant", "content": [TEXT, IMG_URL]},     # old assistant
            {"role": "tool", "content": [TEXT, IMG_URL], "tool_call_id": "t1"},
            {"role": "user", "content": [TEXT, IMG_URL]},          # newest user — keep
        ]
        out = project_historical_attachments(msgs)
        for i in range(3):
            assert not content_has_images(out[i]["content"]), f"msg {i} still has image"
        assert not content_has_images(out[3]["content"])

    def test_text_only_newest_user_still_strips_older_images(self):
        # The anchor is "newest user WITH images". If the newest user is
        # text-only, we fall back to the previous image-bearing user turn.
        msgs = [
            {"role": "user", "content": [TEXT, IMG_URL]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [TEXT, IMG_URL]},  # anchor
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "follow-up text only"},
        ]
        out = project_historical_attachments(msgs)
        # First image-bearing user (index 0) was stripped — it was before the
        # newest image-bearing user (index 2).
        assert not content_has_images(out[0]["content"])
        # The former anchor is projected too; only request-time code may preserve
        # the current attachment for its first call.
        assert not content_has_images(out[2]["content"])

    def test_assistant_image_is_projected_without_user_anchor(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": [TEXT, IMG_URL]},
            {"role": "user", "content": "second"},
        ]
        out = project_historical_attachments(msgs)
        assert out is not msgs
        assert not content_has_images(out[1]["content"])

    def test_does_not_mutate_input_messages(self):
        msg0 = {"role": "user", "content": [TEXT, IMG_URL]}
        msg1 = {"role": "user", "content": [TEXT, IMG_URL]}
        msgs = [msg0, msg1]
        _ = project_historical_attachments(msgs)
        # Originals untouched
        assert content_has_images(msg0["content"])
        assert content_has_images(msg1["content"])

    def test_idempotent(self):
        msgs = [
            {"role": "user", "content": [TEXT, IMG_URL]},
            {"role": "assistant", "content": "k"},
            {"role": "user", "content": [TEXT, IMG_URL]},
        ]
        first = project_historical_attachments(msgs)
        second = project_historical_attachments(first)
        # Second pass is a no-op — no images left before the anchor.
        assert second is first

    def test_non_dict_messages_pass_through(self):
        msgs = [
            "not-a-dict",  # shouldn't crash
            {"role": "user", "content": [TEXT, IMG_URL]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [TEXT, IMG_URL]},
        ]
        out = project_historical_attachments(msgs)
        assert out[0] == "not-a-dict"
        # Image-bearing user at index 1 is before the anchor (index 3) → stripped.
        assert not content_has_images(out[1]["content"])


class TestCompressIntegration:
    """Verify the stripping runs inside ContextCompressor.compress()."""

    @pytest.fixture
    def compressor(self):
        with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
            c = ContextCompressor(
                model="test/model",
                threshold_percent=0.50,
                protect_first_n=1,
                protect_last_n=2,
                quiet_mode=True,
            )
            return c

    def test_compress_strips_historical_images(self, compressor):
        # Enough messages to trigger the summarize path. protect_first_n=1 +
        # protect_last_n=2 + a middle window of at least 3 with a summary.
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [TEXT, IMG_URL]},           # old image-bearing user
            {"role": "assistant", "content": "looked at it"},
            {"role": "user", "content": "follow-up"},
            {"role": "assistant", "content": "ack"},
            {"role": "user", "content": "more"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [TEXT, IMG_URL]},           # newest image-bearing user (tail)
            {"role": "assistant", "content": "done"},
        ]
        # Bypass the real LLM summary — return a stub so compress() proceeds.
        with patch.object(compressor, "_generate_summary", return_value="SUMMARY TEXT"):
            out = compressor.compress(msgs, current_tokens=60_000)

        # Compression emits reusable historical context, so no attachment body
        # survives — including the newest tail image.
        user_imgs = [m for m in out if m.get("role") == "user" and content_has_images(m.get("content"))]
        assert user_imgs == []
        assert "payload omitted" in str(out)
        for m in out:
            assert not content_has_images(m.get("content")), (
                f"Stale image in {m.get('role')!r} message after compression"
            )
