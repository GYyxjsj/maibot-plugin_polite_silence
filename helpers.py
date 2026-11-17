"""礼貌性沉默的纯函数逻辑：时长收拢与指令参数解析。"""

from __future__ import annotations

from typing import Any


def _to_int(value: Any, default: int) -> int:
    """把任意配置值转换为整数；失败时返回默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_ignore_minutes(minutes: Any, min_minutes: Any, max_minutes: Any) -> int:
    """把给出的拒答时长收拢到配置的上下限之间。"""
    lower = max(0, _to_int(min_minutes, 10))
    upper = max(lower, _to_int(max_minutes, 1440))
    return max(lower, min(_to_int(minutes, lower), upper))


def parse_manual_ignore_args(
    target_raw: Any,
    minutes_raw: Any,
    min_minutes: Any,
    max_minutes: Any,
) -> tuple[str, int, str]:
    """解析 `/ignore` 指令的目标与可选时长。

    Returns:
        tuple[str, int, str]: 目标 QQ 号、分钟数，以及错误标识。分钟数为 ``-1``
        表示永久拒答；错误标识为 ``"missing_target"``、``"invalid_target"`` 或
        ``"invalid_minutes"``，成功时为空字符串。
    """
    target_id = str(target_raw or "").strip().strip("[]")
    if not target_id:
        return "", 0, "missing_target"
    if not target_id.isdigit():
        return target_id, 0, "invalid_target"

    raw_minutes = "" if minutes_raw is None else str(minutes_raw).strip()
    if not raw_minutes:
        return target_id, clamp_ignore_minutes(None, min_minutes, max_minutes), ""
    if raw_minutes == "-1":
        return target_id, -1, ""
    if not raw_minutes.isdigit() or int(raw_minutes) <= 0:
        return target_id, 0, "invalid_minutes"
    return target_id, clamp_ignore_minutes(raw_minutes, min_minutes, max_minutes), ""
