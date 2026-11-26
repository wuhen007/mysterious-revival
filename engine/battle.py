from __future__ import annotations

import math
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

from .content_contracts import (
    SUPPORTED_GHOST_BEHAVIOR_KINDS,
    SUPPORTED_GHOST_SKILL_TYPES,
    SUPPORTED_ITEM_EFFECT_TYPES,
    SUPPORTED_BEHAVIOR_TARGET_STRATEGIES,
)
from .ghost_control import (
    MAX_GHOST_SLOTS,
    build_unit,
    capture_probability,
    command_refused,
)
from .recovery import recovery_cost
from .randomness import draw_float
from .rules import observe, reveal_from_counteraction

SOLO_ENEMY_HP_SCALE = 0.35
SOLO_ENEMY_ATTACK_SCALE = 0.5


def order_turn_units(units: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    active = [u for u in units if u.get("side") in {"friendly", "enemy"} and int(u.get("hp", 0)) > 0 and not u.get("rebelled") and not u.get("captured")]
    return sorted(active, key=lambda u: (-int(u.get("speed", 0)), str(u["unit_id"])))


def calculate_damage(attack: float, defense: float, skill_multiplier: float = 1.0, fixed_damage: float = 0, damage_multiplier: float = 1.0) -> int:
    return max(1, math.floor((attack * skill_multiplier + fixed_damage - defense / 2) * damage_multiplier))


def apply_damage(target: MutableMapping[str, Any], damage: int, behavior: Mapping[str, Any] | None = None) -> bool:
    if damage <= 0 or int(target.get("hp", 0)) <= 0:
        return False
    target["hp"] = max(0, int(target["hp"]) - damage)
    counter = (behavior or {}).get("damage_resets_counter")
    if isinstance(counter, str) and target.setdefault("counters", {}).get(counter, 0) > 0:
        target["counters"][counter] = 0
    return True


def ghost_damage_multiplier(target: Mapping[str, Any], behavior: Mapping[str, Any] | None = None) -> float:
    status = target.get("status", {})
    if int(status.get("illuminated", 0)) > 0:
        return float((behavior or {}).get("light_damage_multiplier", status.get("damage_taken_multiplier", 1.5)))
    return float(status.get("damage_taken_multiplier", 1.0))


def infection_hits_at_distance(distance: str) -> bool:
    return distance == "close"


class BattleEngine:
    """Framework-independent deterministic solo battle rules."""

    def __init__(self, registry: Any):
        self.registry = registry

    def start_encounter(self, state: dict[str, Any], encounter_id: str, enemy_refs: Sequence[Mapping[str, Any]]) -> str:
        if state.get("phase") != "explore":
            raise ValueError("a battle can only start during exploration")
        enemy_ids: list[str] = []
        for ref in enemy_refs:
            ghost_id = str(ref.get("ghost_ref", ""))
            entry = self.registry.get("ghosts", ghost_id, state["pack_id"], state["pack_version"])
            if entry is None:
                raise ValueError(f"encounter ghost {ghost_id!r} is missing from pinned content")
            for _ in range(int(ref.get("quantity", 1))):
                unit_id = f"enemy:{encounter_id}:{len(enemy_ids) + 1}"
                unit = build_unit(entry.data, unit_id, "enemy", None)
                if not state.get("party"):
                    # A solo profile has fewer actors; scale runtime units without mutating pinned pack data.
                    unit["max_hp"] = max(1, math.ceil(unit["max_hp"] * SOLO_ENEMY_HP_SCALE))
                    unit["hp"] = unit["max_hp"]
                    unit["attack"] = max(1, math.ceil(unit["attack"] * SOLO_ENEMY_ATTACK_SCALE))
                unit["behavior_overrides"] = dict(ref.get("behavior_overrides", {}))
                state["ghost_units"].append(unit)
                enemy_ids.append(unit_id)
        if not enemy_ids:
            raise ValueError("encounter has no enemies")
        for unit in self._units(state, "friendly"):
            unit["cooldowns"] = {}
        photo_shown = bool(state.get("shared", {}).get("story_flags", {}).get("family_photo_taken"))
        ancestors = [
            unit for unit in state.get("ghost_units", [])
            if unit.get("unit_id") in enemy_ids and unit.get("ghost_id") == "wang_ancestor"
        ] if photo_shown else []
        for ancestor in ancestors:
            status = ancestor.setdefault("status", {})
            status["stunned_actions"] = max(1, int(status.get("stunned_actions", 0)))
        state["phase"] = "battle"
        state["battle"] = {"encounter_id": encounter_id, "status": "active", "round": 1, "turn_order": [], "cursor": 0, "current_actor_id": None, "desperation_used": False, "enemy_unit_ids": enemy_ids, "log": []}
        state["battle"]["turn_order"] = [u["unit_id"] for u in order_turn_units(self._units(state))]
        turn_text = self._advance(state)
        if ancestors:
            return "你亮出全家福，祠堂里的王家亡魂暂时停下脚步。\n" + turn_text
        return turn_text

    def act(self, state: dict[str, Any], profile_id: str, action: Mapping[str, Any]) -> tuple[bool, str, str | None]:
        battle = state.get("battle")
        player = state.get("players", {}).get(profile_id)
        if state.get("phase") != "battle" or not isinstance(battle, dict) or battle.get("status") != "active":
            return False, "当前没有进行中的战斗。", None
        if not isinstance(player, dict) or not player.get("alive", int(player.get("hp", 0)) > 0):
            return False, "你已无法参与当前战斗。", None
        actor_id = battle.get("current_actor_id")
        if not actor_id:
            return False, "当前没有等待指令的行动单位。", None
        kind = str(action.get("type", ""))

        if str(actor_id).startswith("player:"):
            acting_profile = str(actor_id).split(":", 1)[1]
            if acting_profile != profile_id:
                return False, "只有当前绝境选择的队长可以提交指令。", None
            if kind == "retreat":
                escape_succeeded = draw_float(state["rng"]) < float(player.get("escape_chance", 0.5))
                self._count_action(state, profile_id)
                if escape_succeeded:
                    battle["status"], state["phase"] = "retreated", "retreated"
                    return True, "队长成功带领队伍撤离；仅存活成员的合格成果会写入档案。", "ended"
                battle["desperation_used"] = True
                if state.get("party"):
                    state["party"]["desperation_used"] = True
                player["escape_chance"] = 0
                battle["cursor"] += 1
                return True, "绝境撤离失败，机会不会再次出现。\n" + self._advance(state), self._terminal(state)
            if kind == "item":
                invalid = self._prevalidate(state, profile_id, {}, action)
                if invalid:
                    return False, invalid, None
                ok, text = self._use_item(state, profile_id, player, action, None)
                if not ok:
                    return False, text, None
                self._count_action(state, profile_id)
                battle["desperation_used"] = True
                if state.get("party"):
                    state["party"]["desperation_used"] = True
                battle["cursor"] += 1
                return True, text + "\n" + self._advance(state), self._terminal(state)
            return False, "绝境中只能使用道具或尝试撤退。", None

        actor = self._unit(state, str(actor_id))
        if not actor or actor.get("side") != "friendly" or actor.get("owner_profile_id") != profile_id:
            return False, "当前行动驭鬼不属于你。", None
        if action.get("unit_id") and action.get("unit_id") != actor_id:
            return False, "现在不是这只鬼的行动回合。", None
        invalid = self._prevalidate(state, profile_id, actor, action)
        if invalid:
            return False, invalid, None
        if float(actor.get("loyalty", 0)) < 30 and command_refused(float(actor["loyalty"]), draw_float(state["rng"])):
            self._count_action(state, profile_id)
            battle["cursor"] += 1
            return True, f"{actor['name']} 忠诚度过低，拒绝了命令；本次行动已消耗。\n{self._advance(state)}", self._terminal(state)

        if kind == "attack":
            ok, text = self._attack(state, actor, action)
        elif kind == "skill":
            ok, text = self._skill(state, actor, action)
        elif kind == "observe":
            ok, text = self._observe(state, profile_id, actor, action)
        elif kind == "item":
            ok, text = self._use_item(state, profile_id, player, action, actor)
        elif kind == "capture":
            ok, text = self._capture(state, profile_id, actor, action)
        elif kind == "guard":
            player["guard_multiplier"] = 0.5
            ok, text = True, f"{actor['name']} 护在你身前，下一次对你的攻击伤害降低。"
        elif kind == "escort_retreat":
            state["shared"].setdefault("environment", {})["distance"] = "far"
            self._discover_counteraction(state, profile_id, "set_distance_far")
            ok, text = True, f"{actor['name']} 护送你退到远距。"
        elif kind == "skip":
            ok, text = True, f"{actor['name']} 暂时防守，等待下一次行动。"
        elif kind == "retreat":
            if state.get("party"):
                return False, "团队战斗撤退由队长发起。", None
            battle["status"], state["phase"] = "retreated", "retreated"
            return True, "你下令全身撤退；本局临时捕获尚未写入永久档案。", "ended"
        else:
            return False, "未知战斗动作。", None
        if not ok:
            return False, text, None
        self._count_action(state, profile_id)
        battle["cursor"] += 1
        return True, text + ("\n" + self._advance(state) if battle["status"] == "active" else ""), self._terminal(state)

    @staticmethod
    def _count_action(state: dict[str, Any], profile_id: str) -> None:
        if state.get("party"):
            from .party import record_effective_action
            record_effective_action(state, profile_id)
        else:
            player = state.get("players", {}).get(profile_id)
            if player is not None:
                player["actions_taken"] = int(player.get("actions_taken", 0)) + 1

    def _attack(self, state: dict[str, Any], actor: dict[str, Any], action: Mapping[str, Any], power: float = 1.0) -> tuple[bool, str]:
        target = self._target(state, str(action.get("target_id", "")), "enemy")
        if not target:
            return False, "攻击目标无效或已不在战斗中。"
        data = self._ghost_data(state, target)
        behavior = data.get("behavior", {})
        before = dict(target.get("counters", {}))
        damage = calculate_damage(actor["attack"], target["defense"], power, 0, ghost_damage_multiplier(target, behavior))
        apply_damage(target, damage, behavior)
        self._counter_discovery(state, actor, target, data, before)
        return True, f"{actor['name']} 对 {target['name']} 造成 {damage} 点伤害（{target['hp']}/{target['max_hp']} HP）。"

    def _skill(self, state: dict[str, Any], actor: dict[str, Any], action: Mapping[str, Any]) -> tuple[bool, str]:
        data = self._ghost_data(state, actor)
        skill_id = str(action.get("skill_id", ""))
        skill = next((x for x in data.get("skills", []) if x.get("id") == skill_id), None)
        if not skill:
            return False, "这只鬼没有该技能。"
        if int(actor.get("cooldowns", {}).get(skill_id, 0)) > 0:
            return False, "技能仍在冷却中。"
        if skill.get("type", "attack") not in SUPPORTED_GHOST_SKILL_TYPES:
            return False, "该技能类型尚未配置处理器。"
        ok, text = self._attack(state, actor, action, float(skill.get("power", 1)))
        if ok:
            actor.setdefault("cooldowns", {})[skill_id] = int(skill.get("cooldown_turns", 0))
            text = f"{actor['name']} 使用【{skill['name']}】。\n{text}"
            owner_id = str(actor.get("owner_profile_id", ""))
            owned = [
                unit for unit in self._units(state, "friendly")
                if unit.get("owner_profile_id") == owner_id and not unit.get("rebelled")
            ]
            deadlocked = sum(1 for unit in owned if unit.get("deadlocked"))
            if actor.get("deadlocked"):
                text = f"{text}\n（{actor['name']} 已死机，本次行动不产生复苏。）"
            else:
                gain = recovery_cost(int(skill.get("recovery_cost", 2)), len(owned), deadlocked)
                actor["recovery"] = min(100, int(actor.get("recovery", 0)) + gain)
                text = f"{text}\n复苏 +{gain}（{actor['name']} {actor['recovery']}/100）。"
                if actor["recovery"] >= 100:
                    player = state.get("players", {}).get(owner_id)
                    if isinstance(player, dict):
                        player["hp"] = 0
                        player["alive"] = False
                        player["recovery_collapsed"] = True
                    for unit in owned:
                        unit["rebelled"] = True
                    state["phase"] = "dead"
                    text = f"{text}\n复苏失控——{actor['name']} 接管了这具身体，本局结束。"
        return ok, text
    def _observe(self, state: dict[str, Any], profile_id: str, actor: dict[str, Any], action: Mapping[str, Any]) -> tuple[bool, str]:
        target = self._target(state, str(action.get("target_id", "")), "enemy")
        if not target:
            return False, "观察目标无效。"
        rules = self._ghost_data(state, target).get("rules", [])
        rule_id = action.get("rule_id")
        rule = next((r for r in rules if not rule_id or r.get("id") == rule_id), None)
        if not rule:
            return False, "目标没有可观察的规律。"
        key = f"{target['unit_id']}:{rule['id']}"
        evidence = state["shared"].setdefault("rule_evidence", {}).setdefault(key, {"clue_index": 0, "discovered": False})
        player = state["players"][profile_id]
        known = rule["id"] in player.setdefault("discovered_rules", [])
        clue, discovered, san = observe(rule, evidence, int(player["san"]), known)
        player["san"] = san
        if discovered and not known:
            player["discovered_rules"].append(rule["id"])
            state["shared"].setdefault("discovered_rules", []).append(rule["id"])
            return True, f"{actor['name']} 观察到：{clue}\n发现规律【{rule['description']}】，SAN -5（当前 {san}）。"
        return True, f"{actor['name']} 观察到：{clue}"

    def _capture(self, state: dict[str, Any], profile_id: str, actor: dict[str, Any], action: Mapping[str, Any]) -> tuple[bool, str]:
        target = self._target(state, str(action.get("target_id", "")), "enemy")
        if not target:
            return False, "捕获目标无效。"
        player = state["players"][profile_id]
        item_id = str(action.get("item_id", ""))
        bonus = 0.0
        if item_id:
            if int(player["inventory"].get(item_id, 0)) <= 0:
                return False, f"背包中没有 {item_id}。"
            item = self.registry.get("items", item_id, state["pack_id"], state["pack_version"])
            effect = next((e for e in item.data.get("effects", []) if e.get("type") == "capture_bonus"), None) if item else None
            if effect is None:
                return False, "该道具不能用于捕获。"
            bonus = float(effect.get("amount", 0))
        controlled = sum(1 for uid in player["ghost_unit_ids"] if (u := self._unit(state, uid)) and u.get("side") == "friendly" and not u.get("rebelled"))
        data = self._ghost_data(state, target)
        max_slots = max(1, int(player.get("max_ghost_slots", state.get("max_ghost_slots", MAX_GHOST_SLOTS))))
        rate = capture_probability(data, int(target["hp"]), int(target["max_hp"]), bonus, controlled, max_slots)
        if rate is None:
            if controlled >= max_slots:
                return False, "驭鬼槽已满，未消耗行动或封印道具。"
            threshold = int(float(data.get("capture_threshold", 0)) * 100)
            return False, f"目标必须存活且 HP 不高于 {threshold}%，未消耗行动或道具。"
        if item_id:
            player["inventory"][item_id] -= 1
        if draw_float(state["rng"]) < rate:
            target["side"] = "friendly"
            target["owner_profile_id"] = profile_id
            target["loyalty"] = float(data.get("base_loyalty", 50))
            target["captured_round"] = int(state["battle"]["round"])
            player["ghost_unit_ids"].append(target["unit_id"])
            state.setdefault("captured_ghost_ids", []).append(target["ghost_id"])
            state.setdefault("captured_by_profile", {}).setdefault(profile_id, []).append(target["ghost_id"])
            return True, f"成功捕获【{target['name']}】！本局暂时驭使，成功撤离后再写入永久档案。"
        target.setdefault("status", {})["stunned_actions"] = 1
        return True, f"捕获失败（成功率 {rate:.0%}），{target['name']} 停顿一次行动。"

    def _use_item(self, state: dict[str, Any], profile_id: str, player: dict[str, Any], action: Mapping[str, Any], actor: dict[str, Any] | None) -> tuple[bool, str]:
        item_id = str(action.get("item_id", ""))
        if int(player.get("inventory", {}).get(item_id, 0)) <= 0:
            return False, f"背包中没有 {item_id}。"
        entry = self.registry.get("items", item_id, state["pack_id"], state["pack_version"])
        if entry is None:
            return False, "道具内容不存在。"
        effects = entry.data.get("effects", [])
        if any(
            not isinstance(effect.get("type"), str)
            or effect.get("type") not in SUPPORTED_ITEM_EFFECT_TYPES
            for effect in effects
        ):
            return False, "道具包含尚未支持的效果；未消耗行动或道具。"
        if any(e.get("type") == "capture_bonus" for e in effects):
            return False, "封印道具请用 `/鬼域 捕获 <目标> <道具>`。"
        target_id = str(action.get("target_id", "player"))
        target = player if target_id in {"", "player"} else self._unit(state, target_id)
        if target is None:
            return False, "道具目标无效。"
        target_types = entry.data.get("target_types", [])
        if target is player and "player" not in target_types:
            return False, "该道具不能用于玩家本体。"
        if target is not player:
            required = "controlled_ghost" if target.get("side") == "friendly" else "ghost"
            if required not in target_types:
                return False, "该道具不能用于这个目标。"
        if not effects:
            return False, "该道具没有可执行效果。"
        lines: list[str] = []
        for effect in effects:
            kind = effect.get("type")
            amount = float(effect.get("amount", 0))
            if kind == "heal":
                target["hp"] = min(int(target.get("max_hp", target.get("hp", 0))), int(target.get("hp", 0)) + int(amount))
                lines.append(f"恢复 {int(amount)} HP")
            elif kind == "san_change":
                player["san"] = max(0, min(int(player["max_san"]), int(player["san"]) + int(amount)))
                lines.append(f"SAN {amount:+g}")
            elif kind == "loyalty_change" and target is not player:
                target["loyalty"] = max(0, min(100, float(target.get("loyalty", 0)) + amount))
                lines.append(f"忠诚度恢复 {int(amount)}")
            elif kind == "illuminate" and target is not player:
                status = target.setdefault("status", {})
                status["illuminated"] = int(effect.get("turns", 2))
                status["stunned_actions"] = max(int(status.get("stunned_actions", 0)), int(effect.get("skip_actions", 1)))
                status["damage_taken_multiplier"] = float(effect.get("damage_multiplier", 1.5))
                self._discover_counteraction(state, profile_id, "illuminate")
                lines.append("照明持续两轮，目标停顿一次且受击伤害提高 50%")
            elif kind == "apply_status":
                status_name = str(effect.get("status", "effect"))
                target.setdefault("status", {})[status_name] = int(effect.get("turns", 1))
                if status_name == "sealed" and target is not player and target.get("side") == "enemy":
                    self._discover_counteraction(state, profile_id, "golden_seal")
                elif status_name == "sheltered":
                    self._discover_counteraction(state, profile_id, "sheltered")
                lines.append("施加状态")
            elif kind == "damage" and target is not player:
                behavior = self._ghost_data(state, target).get("behavior", {})
                damage = calculate_damage(actor["attack"] if actor else 1, int(target["defense"]), 1, amount)
                apply_damage(target, damage, behavior)
                lines.append(f"造成 {damage} 点伤害")
            else:
                return False, f"尚不支持道具效果 {kind!r}。"
        player["inventory"][item_id] -= 1
        return True, f"使用【{entry.data['name']}】：" + "；".join(lines) + "。"

    def _counter_discovery(self, state: dict[str, Any], actor: Mapping[str, Any], target: Mapping[str, Any], data: Mapping[str, Any], counters_before: Mapping[str, Any]) -> None:
        counter = data.get("behavior", {}).get("damage_resets_counter")
        if not counter or int(counters_before.get(counter, 0)) <= 0:
            return
        profile_id = str(actor.get("owner_profile_id", ""))
        player = state.get("players", {}).get(profile_id)
        if not player:
            return
        for rule in data.get("rules", []):
            if "damage_resets_counter" not in rule.get("counteractions", []):
                continue
            progress = state["shared"].setdefault("rule_evidence", {}).setdefault(f"{target['unit_id']}:{rule['id']}", {"clue_index": 0, "discovered": False})
            known = rule["id"] in player.setdefault("discovered_rules", [])
            if known:
                progress["discovered"] = True
                found, san = False, int(player["san"])
            else:
                found, san = reveal_from_counteraction(rule, progress, int(player["san"]))
            player["san"] = san
            story_flag = rule.get("story_flag_on_counteraction")
            if story_flag:
                state["shared"].setdefault("story_flags", {})[str(story_flag)] = True
            if counter:
                state["shared"].setdefault("rule_counteractions", {})[str(rule["id"])] = True
            if found:
                player["discovered_rules"].append(rule["id"])
                if rule["id"] not in state["shared"].setdefault("discovered_rules", []):
                    state["shared"]["discovered_rules"].append(rule["id"])

    def _discover_counteraction(self, state: dict[str, Any], profile_id: str, action_id: str) -> None:
        player = state["players"][profile_id]
        for enemy in self._units(state, "enemy"):
            for rule in self._ghost_data(state, enemy).get("rules", []):
                if action_id not in rule.get("counteractions", []):
                    continue
                progress = state["shared"].setdefault("rule_evidence", {}).setdefault(f"{enemy['unit_id']}:{rule['id']}", {"clue_index": 0, "discovered": False})
                found, san = reveal_from_counteraction(rule, progress, int(player["san"]))
                player["san"] = san
                story_flag = rule.get("story_flag_on_counteraction")
                if story_flag:
                    state["shared"].setdefault("story_flags", {})[str(story_flag)] = True
                if action_id in rule.get("counteractions", []):
                    state["shared"].setdefault("rule_counteractions", {})[str(rule["id"])] = True
                if found and rule["id"] not in player.setdefault("discovered_rules", []):
                    player["discovered_rules"].append(rule["id"])
                    if rule["id"] not in state["shared"].setdefault("discovered_rules", []):
                        state["shared"]["discovered_rules"].append(rule["id"])
    def _advance(self, state: dict[str, Any]) -> str:
        battle = state["battle"]
        leader_id = state.get("party", {}).get("leader_profile_id") or next(iter(state["players"]))
        lines: list[str] = []
        while battle["status"] == "active":
            living = [
                (profile_id, player)
                for profile_id, player in state["players"].items()
                if int(player.get("hp", 0)) > 0 and player.get("alive", True)
            ]
            if not living:
                battle["status"], state["phase"] = "defeat", "dead"
                lines.append("所有队员均已失去战斗能力，团队副本团灭。" if state.get("party") else "你倒在鬼域中，本局结束。")
                break
            enemies = [u for u in self._units(state, "enemy") if int(u.get("hp", 0)) > 0]
            if not enemies:
                battle["status"], state["phase"] = "victory", "explore"
                for unit in self._units(state, "friendly"):
                    if not unit.get("rebelled"):
                        unit["loyalty"] = min(100, float(unit.get("loyalty", 0)) + 5)
                lines.append("敌鬼已被制服，战斗胜利；己方鬼忠诚度恢复 5。")
                break
            allies = [
                unit for unit in self._units(state, "friendly")
                if int(unit.get("hp", 0)) > 0 and not unit.get("rebelled")
                and int(state["players"].get(unit.get("owner_profile_id"), {}).get("hp", 0)) > 0
                and state["players"].get(unit.get("owner_profile_id"), {}).get("alive", True)
            ]
            desperation_used = bool(battle.get("desperation_used") or state.get("party", {}).get("desperation_used"))
            if not allies and not desperation_used:
                captain = leader_id if any(pid == leader_id for pid, _player in living) else living[0][0]
                battle["current_actor_id"] = f"player:{captain}"
                lines.append("所有驭鬼暂时无法行动，队长进入一次绝境选择：使用道具或尝试撤离。" if state.get("party") else "驭鬼暂时无法行动，进入一次绝境选择：使用道具或尝试撤退。")
                break
            if battle["cursor"] >= len(battle["turn_order"]):
                if not allies and desperation_used:
                    battle["status"], state["phase"] = "defeat", "dead"
                    lines.append("绝境撤离失败，队伍已团灭。" if state.get("party") else "绝境选择失败，敌鬼完成反击，本局结束。")
                    break
                self._finish_round(state, leader_id)
                battle["round"] += 1
                battle["cursor"] = 0
                battle["turn_order"] = [u["unit_id"] for u in order_turn_units(self._units(state))]
                continue
            unit_id = battle["turn_order"][battle["cursor"]]
            unit = self._unit(state, unit_id)
            if not self._unit_can_act(unit, battle, state):
                battle["cursor"] += 1
                continue
            sealed_turns = int(unit.get("status", {}).get("sealed", 0))
            if unit.get("side") == "enemy" and sealed_turns > 0:
                lines.append(f"{unit['name']} 被黄金制品封印，无法行动。")
                self._tick_status(unit)
                battle["cursor"] += 1
                continue
            bound_turns = int(unit.get("status", {}).get("bound", 0))
            if bound_turns > 0:
                if bound_turns == 1:
                    unit["status"].pop("bound", None)
                else:
                    unit["status"]["bound"] = bound_turns - 1
                lines.append(f"{unit['name']} 被裹尸布束缚，无法行动。")
                self._tick_status(unit)
                battle["cursor"] += 1
                continue
            battle["current_actor_id"] = unit_id
            if unit["side"] == "friendly":
                owner = state["players"].get(unit.get("owner_profile_id"), {})
                lines.append(self._player_prompt(state, unit, owner))
                break
            lines.extend(self._enemy_turn(state, unit, leader_id))
            battle["cursor"] += 1
        battle["log"].extend(lines)
        return "\n".join(lines)

    def _enemy_turn(self, state: dict[str, Any], attacker: dict[str, Any], leader_profile_id: str) -> list[str]:
        data = self._ghost_data(state, attacker)
        behavior = data.get("behavior", {})
        behavior_kind = behavior.get("kind", "brute")
        if behavior_kind not in SUPPORTED_GHOST_BEHAVIOR_KINDS:
            raise ValueError(f"unsupported ghost behavior kind {behavior_kind!r}")
        status = attacker.setdefault("status", {})
        if int(status.get("stunned_actions", 0)) > 0:
            status["stunned_actions"] -= 1
            self._tick_status(attacker)
            return [f"{attacker['name']} 受到影响，停顿了一次行动。"]
        lines: list[str] = []
        multiplier = float(behavior.get("attack_multiplier", 1.0))
        if behavior.get("kind") == "knock_counter":
            name = str(behavior.get("counter_name", "knocks"))
            counters = attacker.setdefault("counters", {})
            count = int(counters.get(name, 0)) + 1
            threshold = int(behavior.get("knocks_before_attack", 3))
            counters[name] = count
            lines.append(f"{attacker['name']} 敲门第 {count} 次。")
            if count >= threshold:
                multiplier *= float(behavior.get("strong_attack_multiplier", 1.8))
                counters[name] = 0
                lines.append("强攻袭来！")
        elif behavior.get("kind") == "dark_stalker":
            env = state["shared"].get("environment", {})
            if env.get("light") == "dark" and int(status.get("illuminated", 0)) <= 0:
                multiplier = float(behavior.get("attack_multiplier_in_dark", multiplier))
                lines.append(f"{attacker['name']} 借黑暗迅速逼近！")
        elif behavior.get("kind") == "infection_hunter":
            lines.append(f"{attacker['name']} 咳嗽蓄势，感染袭击即将发动。")

        target_type, target, target_profile_id = self._select_target(state, behavior)
        if target is None:
            self._tick_status(attacker)
            return lines + [f"{attacker['name']} 找不到攻击目标。"]
        discovery_owner = target_profile_id or leader_profile_id
        if behavior.get("kind") == "infection_hunter":
            distance = state["shared"].get("environment", {}).get("distance", "close")
            if distance != behavior.get("required_distance", "close"):
                self._discover_counteraction(state, discovery_owner, "set_distance_far")
                self._tick_status(attacker)
                return lines + [f"你们处于{distance}距离，{attacker['name']} 的感染袭击落空。"]

        defense = int(target.get("defense", 0)) if target_type == "ghost" else 0
        vulnerability = ghost_damage_multiplier(target, behavior) if target_type == "ghost" else 1.0
        damage = calculate_damage(attacker["attack"], defense, multiplier, 0, vulnerability)
        if target_type == "player":
            if behavior.get("supernatural", True) and int(target.get("san", 0)) <= 0:
                damage = math.floor(damage * 1.25)
            damage = max(1, math.floor(damage * float(target.get("guard_multiplier", 1.0))))
            target["guard_multiplier"] = 1.0
            target["hp"] = max(0, int(target["hp"]) - damage)
            if target["hp"] <= 0:
                target["alive"] = False
            lines.append(f"{attacker['name']} 攻击队员 {target.get('user_id', target_profile_id)}，造成 {damage} 点伤害（{target['hp']}/{target['max_hp']} HP）。")
            san_damage = int(behavior.get("infection_san_damage", 0)) if behavior.get("kind") == "infection_hunter" else 0
            if san_damage:
                target["san"] = max(0, int(target["san"]) - san_damage)
                lines.append(f"精神受到冲击，SAN -{san_damage}（当前 {target['san']}）。")
            if behavior.get("kind") == "infection_hunter":
                target.setdefault("status", {})["infection"] = {"damage": int(behavior.get("infection_damage", 5)), "rounds": int(behavior.get("infection_rounds", 2))}
                lines.append("感染已附着，将在后续行动轮持续造成伤害。")
        else:
            apply_damage(target, damage, self._ghost_data(state, target).get("behavior", {}))
            lines.append(f"{attacker['name']} 攻击 {target['name']}，造成 {damage} 点伤害（{target['hp']}/{target['max_hp']} HP）。")
        self._tick_status(attacker)
        return lines

    def _select_target(self, state: dict[str, Any], behavior: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None, str | None]:
        party = state.get("party", {})
        profile_order = party.get("exploration_order", list(state["players"]))
        candidates: list[tuple[str, dict[str, Any], str | None]] = []
        for profile_id in profile_order:
            player = state["players"].get(profile_id)
            if (player and player.get("alive", int(player.get("hp", 0)) > 0)
                    and int(player.get("hp", 0)) > 0
                    and int(player.get("status", {}).get("sealed", 0)) <= 0
                    and int(player.get("status", {}).get("sheltered", 0)) <= 0):
                candidates.append(("player", player, profile_id))
        allies = [
            unit for unit in self._units(state, "friendly")
            if int(unit.get("hp", 0)) > 0 and not unit.get("rebelled")
            and int(unit.get("status", {}).get("sealed", 0)) <= 0
            and int(unit.get("status", {}).get("sheltered", 0)) <= 0
            and int(state["players"].get(unit.get("owner_profile_id"), {}).get("hp", 0)) > 0
            and state["players"].get(unit.get("owner_profile_id"), {}).get("alive", True)
        ]
        candidates.extend(("ghost", unit, str(unit.get("owner_profile_id", ""))) for unit in allies)
        if not candidates:
            return "", None, None
        strategy = behavior.get("target_strategy", "weakest")
        if not isinstance(strategy, str) or strategy not in SUPPORTED_BEHAVIOR_TARGET_STRATEGIES:
            raise ValueError(f"unsupported behavior target strategy {strategy!r}")
        if strategy == "player_first":
            return next((x for x in candidates if x[0] == "player"), candidates[0])
        if strategy == "ghost_first":
            return next((x for x in candidates if x[0] == "ghost"), candidates[0])
        if strategy == "random":
            return candidates[min(int(draw_float(state["rng"]) * len(candidates)), len(candidates) - 1)]
        return min(candidates, key=lambda x: (int(x[1].get("hp", 0)) / max(1, int(x[1].get("max_hp", 1))), x[0], str(x[1].get("unit_id", x[2] or ""))))

    def _finish_round(self, state: dict[str, Any], _leader_profile_id: str) -> None:
        for target in [*state.get("players", {}).values(), *self._units(state)]:
            status = target.get("status", {})
            for status_name in ("sealed", "sheltered"):
                if int(status.get(status_name, 0)) > 0:
                    status[status_name] -= 1
                    if status[status_name] <= 0:
                        status.pop(status_name, None)
        for unit in self._units(state, "friendly"):
            owner = state["players"].get(unit.get("owner_profile_id"), {})
            if int(unit.get("hp", 0)) <= 0 or unit.get("rebelled") or unit.get("deadlocked") or int(owner.get("hp", 0)) <= 0 or not owner.get("alive", True):
                continue
            unit["loyalty"] = max(0, float(unit.get("loyalty", 0)) - 2)
            for skill_id, cooldown in unit.get("cooldowns", {}).items():
                unit["cooldowns"][skill_id] = max(0, int(cooldown) - 1)
            if unit["loyalty"] <= 0:
                unit["rebelled"] = True
                owner = state["players"].get(unit.get("owner_profile_id"), {})
                owner["ghost_unit_ids"] = [uid for uid in owner.get("ghost_unit_ids", []) if uid != unit["unit_id"]]
        for player in state.get("players", {}).values():
            infection = player.get("status", {}).get("infection")
            if infection and infection["rounds"] > 0 and int(player.get("hp", 0)) > 0:
                player["hp"] = max(0, int(player["hp"]) - int(infection["damage"]))
                infection["rounds"] -= 1
                if player["hp"] <= 0:
                    player["alive"] = False
                if infection["rounds"] <= 0:
                    player["status"].pop("infection", None)

    def _player_prompt(self, state: Mapping[str, Any], actor: Mapping[str, Any], player: Mapping[str, Any]) -> str:
        enemies = [u for u in self._units(state, "enemy") if int(u.get("hp", 0)) > 0]
        enemy_text = "；".join(f"{u['name']} {u['hp']}/{u['max_hp']} HP" for u in enemies) or "无"
        clues: list[str] = []
        for enemy in enemies:
            for rule in self._ghost_data(state, enemy).get("rules", []):
                evidence = state["shared"].get("rule_evidence", {}).get(f"{enemy['unit_id']}:{rule['id']}", {})
                if evidence.get("discovered") or rule["id"] in player.get("discovered_rules", []):
                    clues.append(f"已知规律：{rule['description']}")
                else:
                    clue_index = int(evidence.get("clue_index", 0))
                    clue_list = rule.get("clues", [])
                    if clue_index < len(clue_list):
                        clues.append(f"待调查线索：{clue_list[clue_index]}")
        clue_text = "\n".join(clues) if clues else "尚无规律线索。"
        recovery_text = "已死机" if actor.get("deadlocked") else f"{int(actor.get('recovery', 0))}/100"
        return f"轮到【{actor['name']}】（忠诚度 {actor.get('loyalty', 0):.0f}，复苏 {recovery_text}）。你 HP {player['hp']}/{player['max_hp']}、SAN {player['san']}/{player['max_san']}；敌方：{enemy_text}。\n{clue_text}\n可攻击、使用技能、观察、用道具、捕获、护主或跳过；复苏 ≥70 可尝试 `/鬼域 死机 <鬼名>`。"

    def _unit_can_act(self, unit: dict[str, Any] | None, battle: Mapping[str, Any], state: Mapping[str, Any] | None = None) -> bool:
        if not unit or int(unit.get("hp", 0)) <= 0 or unit.get("captured") or unit.get("rebelled"):
            return False
        if unit.get("side") == "friendly" and state is not None:
            owner = state.get("players", {}).get(unit.get("owner_profile_id"), {})
            if int(owner.get("hp", 0)) <= 0 or not owner.get("alive", True):
                return False
        return not (unit.get("side") == "friendly" and int(unit.get("captured_round", 0)) == int(battle["round"]))

    @staticmethod
    def _tick_status(unit: dict[str, Any]) -> None:
        status = unit.setdefault("status", {})
        if int(status.get("illuminated", 0)) > 0:
            status["illuminated"] -= 1
            if status["illuminated"] <= 0:
                status.pop("illuminated", None)
                status.pop("damage_taken_multiplier", None)

    def _target(self, state: Mapping[str, Any], target_id: str, side: str) -> dict[str, Any] | None:
        return next((u for u in state.get("ghost_units", []) if u.get("unit_id") == target_id and u.get("side") == side and int(u.get("hp", 0)) > 0), None)

    @staticmethod
    def _unit(state: Mapping[str, Any], unit_id: str) -> dict[str, Any] | None:
        return next((u for u in state.get("ghost_units", []) if u.get("unit_id") == unit_id), None)

    def _units(self, state: Mapping[str, Any], side: str | None = None) -> list[dict[str, Any]]:
        return [u for u in state.get("ghost_units", []) if side is None or u.get("side") == side]

    def _ghost_data(self, state: Mapping[str, Any], unit: Mapping[str, Any]) -> dict[str, Any]:
        pack_id = str(unit.get("ghost_pack_id") or state["pack_id"])
        pack_version = str(unit.get("ghost_pack_version") or state["pack_version"])
        entry = self.registry.get("ghosts", unit["ghost_id"], pack_id, pack_version)
        if entry is None:
            raise ValueError(f"ghost {unit['ghost_id']!r} is missing from pinned content {pack_id}@{pack_version}")
        overrides = unit.get("behavior_overrides", {})
        if not overrides:
            return entry.data
        merged = dict(entry.data)
        merged["behavior"] = {**entry.data.get("behavior", {}), **overrides}
        return merged

    @staticmethod
    def _terminal(state: Mapping[str, Any]) -> str | None:
        return "ended" if state.get("phase") in {"dead", "retreated"} else None
    def _prevalidate(self, state: dict[str, Any], profile_id: str, actor: Mapping[str, Any], action: Mapping[str, Any]) -> str:
        kind = str(action.get("type", ""))
        if kind in {"guard", "escort_retreat", "skip", "retreat"}:
            return ""
        if kind in {"attack", "skill", "capture"} and not self._target(state, str(action.get("target_id", "")), "enemy"):
            return "目标无效或已不在战斗中。"
        if kind == "skill":
            data = self._ghost_data(state, actor)
            skill = next((x for x in data.get("skills", []) if x.get("id") == action.get("skill_id")), None)
            if not skill:
                return "这只鬼没有该技能。"
            if skill.get("type", "attack") not in SUPPORTED_GHOST_SKILL_TYPES:
                return "该技能类型尚未配置处理器。"
            if int(actor.get("cooldowns", {}).get(skill["id"], 0)) > 0:
                return "技能仍在冷却中。"
        if kind == "observe":
            target = self._target(state, str(action.get("target_id", "")), "enemy")
            if not target:
                return "观察目标无效。"
            rules = self._ghost_data(state, target).get("rules", [])
            if not any(not action.get("rule_id") or r.get("id") == action.get("rule_id") for r in rules):
                return "目标没有可观察的规律。"
        if kind == "capture":
            player = state["players"][profile_id]
            target = self._target(state, str(action.get("target_id", "")), "enemy")
            data = self._ghost_data(state, target)
            item_id = str(action.get("item_id", ""))
            bonus = 0.0
            if item_id:
                if int(player["inventory"].get(item_id, 0)) <= 0:
                    return f"背包中没有 {item_id}。"
                item = self.registry.get("items", item_id, state["pack_id"], state["pack_version"])
                effect = next((e for e in item.data.get("effects", []) if e.get("type") == "capture_bonus"), None) if item else None
                if effect is None:
                    return "该道具不能用于捕获。"
                bonus = float(effect.get("amount", 0))
            count = sum(1 for uid in player["ghost_unit_ids"] if (u := self._unit(state, uid)) and u.get("side") == "friendly" and not u.get("rebelled"))
            max_slots = max(1, int(player.get("max_ghost_slots", state.get("max_ghost_slots", MAX_GHOST_SLOTS))))
            if count >= max_slots:
                return "驭鬼槽已满；未消耗行动或封印道具。"
            if capture_probability(data, int(target["hp"]), int(target["max_hp"]), bonus, count, max_slots) is None:
                return "目标尚未达到可捕获 HP 门槛；未消耗行动或封印道具。"
        if kind == "item":
            player = state["players"][profile_id]
            item_id = str(action.get("item_id", ""))
            if int(player.get("inventory", {}).get(item_id, 0)) <= 0:
                return f"背包中没有 {item_id}。"
            entry = self.registry.get("items", item_id, state["pack_id"], state["pack_version"])
            if not entry:
                return "道具内容不存在。"
            if any(e.get("type") == "capture_bonus" for e in entry.data.get("effects", [])):
                return "封印道具请用 `/鬼域 捕获 <目标> <道具>`。"
            target_id = str(action.get("target_id", "player"))
            target = player if target_id in {"", "player"} else self._unit(state, target_id)
            if not target:
                return "道具目标无效。"
            required = "player" if target is player else ("controlled_ghost" if target.get("side") == "friendly" else "ghost")
            if required not in entry.data.get("target_types", []):
                return "该道具不能用于这个目标。"
            if any(
                not isinstance(effect.get("type"), str)
                or effect.get("type") not in SUPPORTED_ITEM_EFFECT_TYPES
                for effect in entry.data.get("effects", [])
            ):
                return "道具包含尚未支持的效果；未消耗行动或道具。"
        if kind not in {"attack", "skill", "observe", "item", "capture", "guard", "escort_retreat", "skip", "retreat"}:
            return "未知战斗动作。"
        return ""

