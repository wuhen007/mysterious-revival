"""每日行动预算：按驭鬼数量决定上限，跨日自动重置。"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from datetime import date
from typing import Any

from .recovery import DEFAULT_CONFIG, RecoveryConfig, action_cap


def today() -> str:
    return date.today().isoformat()


def budget(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("daily")
    if not isinstance(raw, dict):
        return {"date": today(), "used": 0}
    return {"date": str(raw.get("date", "")), "used": int(raw.get("used", 0) or 0)}


def remaining(
    payload: Mapping[str, Any],
    ghost_count: int,
    all_deadlocked: bool = False,
    config: RecoveryConfig = DEFAULT_CONFIG,
) -> int:
    state = budget(payload)
    cap = action_cap(ghost_count, all_deadlocked, config)
    used = state["used"] if state["date"] == today() else 0
    return max(0, cap - used)


def consume(
    payload: MutableMapping[str, Any],
    ghost_count: int,
    all_deadlocked: bool = False,
    amount: int = 1,
    config: RecoveryConfig = DEFAULT_CONFIG,
) -> tuple[bool, str]:
    """扣行动次数；跨日先重置。返回 (是否成功, 提示)。"""
    state = budget(payload)
    if state["date"] != today():
        state = {"date": today(), "used": 0}
    cap = action_cap(ghost_count, all_deadlocked, config)
    need = max(1, int(amount))
    if state["used"] + need > cap:
        left = max(0, cap - state["used"])
        limit = str(cap)
        return False, f"今日行动已用尽（{state['used']}/{limit}，剩余 {left}）。每日按本地日期重置，请明天再继续。"
    state["used"] += need
    payload["daily"] = state
    return True, ""


def used_today(payload: Mapping[str, Any]) -> int:
    state = budget(payload)
    return state["used"] if state["date"] == today() else 0


def summary(payload: Mapping[str, Any], ghost_count: int, all_deadlocked: bool = False) -> str:
    cap = action_cap(ghost_count, all_deadlocked)
    used = used_today(payload)
    limit = str(cap)
    return f"行动 {used}/{limit}"
