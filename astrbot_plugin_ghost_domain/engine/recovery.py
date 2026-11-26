"""厉鬼复苏 · 死机 · 行动次数 · 寿命：数值规则与状态流转。

设计依据（知识库）：
- W-07 厉鬼复苏：用得越多复苏越快，复苏不可逆，死后厉鬼复苏形成新事件。
- W-08 驭鬼者分级：一只＝普通、两只＝一流、三只＝队长级、四只以上极少数。
- W-09 完美驾驭与「死机」：让鬼彻底复苏，再以另一只鬼与之冲突，制造死锁，人成为唯一赢家。
- W-14 第四种存在：以自身为资粮，摆脱复苏倒计时。

全部为纯函数，便于单测、模拟与后续调参。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RecoveryConfig:
    # 行动次数
    base_actions: int = 6
    unlimited_actions: int = 999
    unlimited_from_ghosts: int = 6
    # 复苏
    base_skill_cost: int = 2
    pressure_per_extra_ghost: float = 0.35
    deadlock_relief: float = 0.5
    daily_tick_from_ghosts: int = 3
    collapse_threshold: int = 100
    critical_threshold: int = 80
    # 死机
    deadlock_threshold: int = 70
    deadlock_action_cost: int = 3
    deadlock_san_cost: int = 20
    deadlock_fail_recovery: int = 15
    deadlock_fail_loyalty: int = 10
    deadlock_min_conflict: float = 0.5
    deadlock_min_chance: float = 0.05
    deadlock_max_chance: float = 0.95
    # 寿命（天）
    lifespan_one_ghost: int = 90
    lifespan_two_ghosts_min: int = 365
    lifespan_two_ghosts_bonus: int = 730
    lifespan_grace_three_plus: int = 90
    lifespan_per_deadlock_pair: int = 365
    lifespan_unlimited: int = 9999


DEFAULT_CONFIG = RecoveryConfig()

# 鬼的「本质」标签：用于判定冲突强度。未列出的按 behavior.kind 兜底。
ESSENCE: dict[str, str] = {
    "door_knocker": "door",
    "door_opener": "door",
    "ghost_official": "seal",
    "ghost_coffin": "seal",
    "post_office": "door",
    "ghost_lake": "water",
    "ghost_rain": "water",
    "ghost_fire": "fire",
    "ghost_baby": "dark",
    "headless_shadow": "shadow",
    "ghost_shadow_full": "shadow",
    "ghost_shadow_head": "shadow",
    "ghost_mirror_item": "mirror",
    "ghost_painting": "image",
    "ghost_camera": "image",
    "room_301": "time",
    "pendulum_clock": "time",
    "meng_clone": "time",
    "nightmare": "dream",
    "ghost_phase": "memory",
    "ghost_newspaper": "memory",
    "ghost_liar": "memory",
    "puppet_master": "copy",
    "ghost_makeup": "copy",
    "grave_field": "bury",
    "hungry_ghost": "grow",
    "ghost_child": "grow",
    "spirit_child": "foresee",
    "substitute_ghost": "replace",
    "sick_ghost": "plague",
    "holy_water": "heal",
    "ghost_blood": "suppress",
    "ghost_shroud": "isolate",
    "gold_box": "isolate",
}

_BEHAVIOR_ESSENCE = {
    "knock_counter": "door",
    "dark_stalker": "dark",
    "infection_hunter": "plague",
    "brute": "force",
}

# 冲突强度表（对称）。数值越高，越容易触发死机。
OPPOSITES: dict[frozenset[str], float] = {
    frozenset({"light", "dark"}): 0.90,
    frozenset({"door", "seal"}): 0.85,
    frozenset({"bury", "grow"}): 0.90,
    frozenset({"copy", "unique"}): 0.80,
    frozenset({"time", "now"}): 0.85,
    frozenset({"memory", "blank"}): 0.90,
    frozenset({"plague", "heal"}): 0.70,
    frozenset({"force", "evade"}): 0.60,
    frozenset({"space", "anchor"}): 0.70,
    frozenset({"fire", "water"}): 0.75,
    frozenset({"shadow", "light"}): 0.85,
    frozenset({"mirror", "unique"}): 0.75,
    frozenset({"image", "blank"}): 0.80,
}


def essence_of(ghost_id: str, behavior_kind: str = "brute") -> str:
    return ESSENCE.get(ghost_id) or _BEHAVIOR_ESSENCE.get(behavior_kind, "force")


def conflict_strength(essence_a: str, essence_b: str) -> float:
    if not essence_a or not essence_b:
        return 0.0
    if essence_a == essence_b:
        return 0.0
    return OPPOSITES.get(frozenset({essence_a, essence_b}), 0.0)


# ---------------------------------------------------------------- 行动次数
def action_cap(ghost_count: int, all_deadlocked: bool = False, config: RecoveryConfig = DEFAULT_CONFIG) -> int:
    """每日行动上限：6 + n(n+1)；n>=6 或全员死机 → 999 次/日。"""
    if all_deadlocked:
        return config.unlimited_actions
    if ghost_count >= config.unlimited_from_ghosts:
        return config.unlimited_actions
    count = max(0, int(ghost_count))
    return config.base_actions + count * (count + 1)


# ---------------------------------------------------------------- 复苏
def recovery_cost(
    base: int = 2,
    ghost_count: int = 1,
    deadlocked_count: int = 0,
    config: RecoveryConfig = DEFAULT_CONFIG,
) -> int:
    """单次动用鬼力的复苏值：随驭鬼数量上升，死机鬼提供减负。"""
    count = max(1, int(ghost_count))
    deadlocked = max(0, min(count, int(deadlocked_count)))
    pressure = 1 + config.pressure_per_extra_ghost * (count - 1)
    relief = 1 - config.deadlock_relief * (deadlocked / count)
    return max(1, round(base * pressure * relief))


def daily_recovery(ghost_count: int, deadlocked_count: int, config: RecoveryConfig = DEFAULT_CONFIG) -> int:
    """每天自然复苏：3 只以上且未全员死机时，每只未死机的鬼 +1。"""
    count = max(0, int(ghost_count))
    if count < config.daily_tick_from_ghosts:
        return 0
    return max(0, count - max(0, int(deadlocked_count)))


def collapse_check(unit: Mapping[str, Any], config: RecoveryConfig = DEFAULT_CONFIG) -> bool:
    return int(unit.get("recovery", 0)) >= config.collapse_threshold


def critical(unit: Mapping[str, Any], config: RecoveryConfig = DEFAULT_CONFIG) -> bool:
    return int(unit.get("recovery", 0)) >= config.critical_threshold


# ---------------------------------------------------------------- 死机
def deadlock_chance(
    recovery: int,
    conflict: float,
    san: int,
    config: RecoveryConfig = DEFAULT_CONFIG,
) -> float:
    """成功概率 = 0.20 + 0.45×复苏率 + 0.30×冲突强度 − SAN 过低惩罚。"""
    chance = 0.20 + 0.45 * (max(0, min(100, recovery)) / 100) + 0.30 * max(0.0, min(1.0, conflict))
    if san < 30:
        chance -= 0.25
    return max(config.deadlock_min_chance, min(config.deadlock_max_chance, chance))


def deadlock_blockers(
    unit: Mapping[str, Any] | None,
    partner: Mapping[str, Any] | None,
    conflict: float,
    config: RecoveryConfig = DEFAULT_CONFIG,
) -> str:
    """返回不可死机的原因；空字符串表示可以尝试。"""
    if not unit:
        return "目标驭鬼不存在。"
    if unit.get("deadlocked"):
        return f"{unit.get('name', '该鬼')} 已经死机，无法再次死机。"
    if int(unit.get("recovery", 0)) >= config.collapse_threshold:
        return f"{unit.get('name', '该鬼')} 已经彻底复苏，来不及死机了。"
    if int(unit.get("recovery", 0)) < config.deadlock_threshold:
        return (
            f"{unit.get('name', '该鬼')} 的复苏值只有 {int(unit.get('recovery', 0))}"
            f"/{config.deadlock_threshold}，还没到能死机的程度。"
        )
    if not partner:
        return "没有可用于制造冲突的另一只鬼。"
    if conflict < config.deadlock_min_conflict:
        return "两只鬼之间没有足够的规则冲突，无法形成死锁。"
    return ""


def apply_deadlock(unit: dict[str, Any], recovery_target: int = 0) -> None:
    """把一只鬼置为死机：冻结复苏、清空计数、忠诚固定。"""
    unit["deadlocked"] = True
    unit["recovery"] = recovery_target
    unit["recovery_frozen"] = True
    unit["loyalty"] = max(float(unit.get("loyalty", 60)), 60.0)
    unit["rebelled"] = False
    unit.setdefault("status", {})["deadlock"] = 1


def mark_deadlock_failure(unit: dict[str, Any], config: RecoveryConfig = DEFAULT_CONFIG) -> None:
    unit["recovery"] = min(
        config.collapse_threshold,
        int(unit.get("recovery", 0)) + config.deadlock_fail_recovery,
    )
    unit["loyalty"] = max(0.0, float(unit.get("loyalty", 50)) - config.deadlock_fail_loyalty)


# ---------------------------------------------------------------- 寿命
def balance_score(essences: Sequence[str]) -> float:
    """两只鬼的平衡度：0＝强冲突，1＝完全互补。"""
    if len(essences) < 2:
        return 1.0
    conflict = 0.0
    pairs = 0
    for index, left in enumerate(essences):
        for right in essences[index + 1 :]:
            conflict = max(conflict, conflict_strength(left, right))
            pairs += 1
    return 1.0 - (conflict if pairs else 0.0)


def lifespan_days(
    ghost_count: int,
    balance: float = 1.0,
    deadlocked_pairs: int = 0,
    all_deadlocked: bool = False,
    config: RecoveryConfig = DEFAULT_CONFIG,
) -> int:
    """寿命（天）：
    - 1 鬼：90 天
    - 2 鬼：365 + 730×平衡度（365–1095）
    - 3 鬼以上：90 天宽限 + 365×死机对数；全员死机 → 9999（无限制）
    """
    if all_deadlocked:
        return config.lifespan_unlimited
    count = max(1, int(ghost_count))
    if count == 1:
        return config.lifespan_one_ghost
    if count == 2:
        return int(config.lifespan_two_ghosts_min + config.lifespan_two_ghosts_bonus * max(0.0, min(1.0, balance)))
    return config.lifespan_grace_three_plus + config.lifespan_per_deadlock_pair * max(0, int(deadlocked_pairs))


def lifespan_label(days: int, config: RecoveryConfig = DEFAULT_CONFIG) -> str:
    if days >= config.lifespan_unlimited:
        return "无限制"
    if days >= 365:
        return f"约 {days / 365:.1f} 年"
    return f"约 {days} 天"


# ---------------------------------------------------------------- 状态流转
STATE_NORMAL = "normal"
STATE_CRITICAL = "critical"
STATE_DEADLOCKING = "deadlocking"
STATE_DEADLOCKED = "deadlocked"
STATE_COLLAPSED = "collapsed"


def next_state(unit: Mapping[str, Any], config: RecoveryConfig = DEFAULT_CONFIG) -> str:
    """单只鬼的状态机：normal → critical → deadlocking → deadlocked；满值 → collapsed。"""
    if unit.get("deadlocked"):
        return STATE_DEADLOCKED
    if int(unit.get("recovery", 0)) >= config.collapse_threshold:
        return STATE_COLLAPSED
    if unit.get("deadlocking"):
        return STATE_DEADLOCKING
    if int(unit.get("recovery", 0)) >= config.critical_threshold:
        return STATE_CRITICAL
    return STATE_NORMAL


def state_label(state: str) -> str:
    return {
        STATE_NORMAL: "稳定",
        STATE_CRITICAL: "濒临复苏",
        STATE_DEADLOCKING: "死机中",
        STATE_DEADLOCKED: "已死机",
        STATE_COLLAPSED: "复苏失控",
    }.get(state, state)
