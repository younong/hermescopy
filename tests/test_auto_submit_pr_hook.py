import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "auto-submit-pr.py"

spec = importlib.util.spec_from_file_location("auto_submit_pr", HOOK)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_skips_missing_session_id(tmp_path):
    result = module.process({}, {"CLAUDE_AUTO_PR_STATE_DIR": str(tmp_path)})
    assert "缺少 session_id" in result["systemMessage"]


def test_skips_non_repository(tmp_path):
    result = module.process(
        {"session_id": "one", "cwd": str(tmp_path)},
        {"CLAUDE_AUTO_PR_STATE_DIR": str(tmp_path / "state")},
    )
    assert "不是有效的 Git worktree" in result["systemMessage"]


def test_result_is_stop_hook_json(tmp_path):
    result = module._result("message")
    assert result["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert result["hookSpecificOutput"]["additionalContext"] == "message"
    json.dumps(result, ensure_ascii=False)


def test_conflict_detection():
    assert module._has_conflicts("UU file.py\n")
    assert not module._has_conflicts(" M file.py\n?? new.py\n")


def test_commit_message_is_bounded():
    message = module._commit_message("feature/" + "x" * 200)
    assert message.startswith("Auto-submit: ")
    assert len(message) <= len("Auto-submit: ") + 70
