from helpers import clamp_ignore_minutes, parse_manual_ignore_args


def test_clamp_ignore_minutes():
    assert clamp_ignore_minutes(30, 10, 1440) == 30
    assert clamp_ignore_minutes(5, 10, 1440) == 10
    assert clamp_ignore_minutes(5000, 10, 1440) == 1440
    assert clamp_ignore_minutes("abc", 10, 1440) == 10


def test_clamp_ignore_minutes_normalizes_invalid_bounds():
    assert clamp_ignore_minutes(30, 20, 10) == 20
    assert clamp_ignore_minutes(30, -5, 100) == 30


def test_manual_ignore_defaults_to_min_minutes():
    assert parse_manual_ignore_args("10001", "", 10, 1440) == ("10001", 10, "")
    assert parse_manual_ignore_args("10001", None, 10, 1440) == ("10001", 10, "")


def test_manual_ignore_clamps_minutes():
    assert parse_manual_ignore_args("10001", "5", 10, 60) == ("10001", 10, "")
    assert parse_manual_ignore_args("10001", "120", 10, 60) == ("10001", 60, "")


def test_manual_ignore_strips_brackets():
    assert parse_manual_ignore_args("[10001]", "30", 10, 60) == ("10001", 30, "")


def test_manual_ignore_minus_one_is_permanent():
    assert parse_manual_ignore_args("10001", "-1", 10, 60) == ("10001", -1, "")


def test_manual_ignore_rejects_invalid_minutes():
    for minutes in ("abc", "0", "-5", "1.5"):
        assert parse_manual_ignore_args("10001", minutes, 10, 60) == (
            "10001",
            0,
            "invalid_minutes",
        )


def test_manual_ignore_requires_target():
    assert parse_manual_ignore_args("", "30", 10, 60) == ("", 0, "missing_target")
    assert parse_manual_ignore_args("  ", "", 10, 60) == ("", 0, "missing_target")


def test_manual_ignore_requires_qq_number():
    assert parse_manual_ignore_args("昵称", "30", 10, 60) == (
        "昵称",
        0,
        "invalid_target",
    )
    assert parse_manual_ignore_args("[abc]", "30", 10, 60) == (
        "abc",
        0,
        "invalid_target",
    )
