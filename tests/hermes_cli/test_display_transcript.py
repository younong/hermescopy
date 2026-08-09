"""Tests for persisted transcript display conversion."""

from hermes_cli.display_transcript import format_display_transcript


def test_tool_output_is_preserved_for_history_artifact_recovery():
    messages = format_display_transcript([
        {
            "content": "Full output saved to: /workspace/user3/generated/report.html",
            "role": "tool",
            "tool_call_id": "tool-1",
            "tool_name": "write_file",
        },
    ])

    assert messages == [{
        "role": "tool",
        "name": "write_file",
        "context": "",
        "text": "Full output saved to: /workspace/user3/generated/report.html",
        "tool_call_id": "tool-1",
    }]


def test_empty_tool_output_keeps_existing_display_shape():
    messages = format_display_transcript([
        {"content": "", "role": "tool", "tool_name": "write_file"},
    ])

    assert messages == [{"role": "tool", "name": "write_file", "context": ""}]


def test_image_tool_output_remains_an_image_artifact():
    messages = format_display_transcript([
        {
            "content": '{"success": true, "host_image": "/workspace/cat.png"}',
            "role": "tool",
            "tool_call_id": "image-1",
            "tool_name": "image_generate",
        },
        {
            "content": "Here is the image.",
            "role": "assistant",
        },
    ])

    assert messages == [{
        "role": "assistant",
        "text": "Here is the image.",
        "content": [{
            "type": "artifact.image",
            "url": "/workspace/cat.png",
            "title": "Generated image",
            "tool_call_id": "image-1",
        }],
    }]
