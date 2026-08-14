import pytest

from hermes_cli.collaboration.parser import parse_discussion_round_count


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Discuss for 3 rounds", 3),
        ("Discuss ３ rounds", 3),
        ("请讨论3轮", 3),
        ("请讨论三轮", 3),
        ("请讨论十轮", 10),
        ("Round 2: continue", 2),
        ("There are 3 topics to discuss", 1),
        ("充分讨论直到达成共识", 1),
        ("No explicit count", 1),
    ],
)
def test_parse_discussion_round_count(text, expected):
    assert parse_discussion_round_count(text) == expected


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("先讨论 2 轮，再讨论 3 轮", "conflicting"),
        ("Discuss for 11 rounds", "between 1 and 10"),
        ("讨论零轮", "between 1 and 10"),
    ],
)
def test_parse_discussion_round_count_rejects_invalid_requests(text, message):
    with pytest.raises(ValueError, match=message):
        parse_discussion_round_count(text)
