from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .content_contracts import (
    SUPPORTED_STORY_CONDITION_TYPES,
    SUPPORTED_STORY_EFFECT_TYPES,
)

_DIFFICULTY_ORDER = {"normal": 0, "hard": 1, "nightmare": 2}


def conditions_met(
    conditions: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
    profile_id: str,
    location_id: str = "",
) -> bool:
    player = state.get("players", {}).get(profile_id, {})
    shared = state.get("shared", {})
    location_id = location_id or _current_location_id(state)
    for condition in conditions:
        kind = condition.get("type")
        if not isinstance(kind, str) or kind not in SUPPORTED_STORY_CONDITION_TYPES:
            return False
        if kind == "owns_item":
            if int(player.get("inventory", {}).get(condition.get("item_ref"), 0)) < int(condition.get("quantity", 1)):
                return False
        elif kind == "hp_below":
            hp = int(player.get("hp", 0))
            maximum = max(1, int(player.get("max_hp", 1)))
            if hp / maximum >= float(condition.get("ratio", 0.5)):
                return False
        elif kind == "rule_discovered":
            rule_id = condition.get("rule_ref")
            if rule_id not in player.get("discovered_rules", []) and rule_id not in shared.get("discovered_rules", []):
                return False
        elif kind == "rule_countered":
            if not shared.get("rule_counteractions", {}).get(condition.get("rule_ref"), False):
                return False
        elif kind == "story_flag_matches":
            if shared.get("story_flags", {}).get(condition.get("flag")) != condition.get("value"):
                return False
        elif kind == "difficulty_at_least":
            current = _DIFFICULTY_ORDER.get(str(state.get("difficulty", "normal")), -1)
            required = _DIFFICULTY_ORDER.get(str(condition.get("difficulty", "normal")), 0)
            if current < required:
                return False
        elif kind == "counter_at_least":
            if int(shared.get("counters", {}).get(condition.get("counter"), 0)) < int(condition.get("value", 1)):
                return False
        elif kind == "location_is":
            current_location = location_id or _current_location_id(state)
            if current_location != condition.get("location_ref"):
                return False
        else:
            return False
    return True


def apply_run_effects(
    effects: Sequence[Mapping[str, Any]], state: dict[str, Any], profile_id: str
) -> None:
    player = state.setdefault("players", {}).setdefault(profile_id, {})
    shared = state.setdefault("shared", {})
    flags = shared.setdefault("story_flags", {})
    inventory = player.setdefault("inventory", {})
    for effect in effects:
        kind = effect.get("type")
        if not isinstance(kind, str) or kind not in SUPPORTED_STORY_EFFECT_TYPES:
            raise ValueError(f"unsupported story effect {kind!r}")
        amount = int(effect.get("amount", 0))
        if kind == "set_story_flag":
            flags[str(effect["flag"])] = effect.get("value", True)
        elif kind == "reveal_rule":
            rule_id = str(effect["rule_ref"])
            shared_rules = shared.setdefault("discovered_rules", [])
            personal_rules = player.setdefault("discovered_rules", [])
            if rule_id not in shared_rules and rule_id not in personal_rules:
                cost = int(effect.get("san_cost", 5))
                player["san"] = max(0, int(player.get("san", 0)) - cost)
            if rule_id not in shared_rules:
                shared_rules.append(rule_id)
            if rule_id not in personal_rules:
                personal_rules.append(rule_id)
        elif kind == "item_add":
            item_id = str(effect["item_ref"])
            inventory[item_id] = int(inventory.get(item_id, 0)) + int(effect.get("quantity", 1))
        elif kind == "item_remove":
            item_id = str(effect["item_ref"])
            quantity = int(effect.get("quantity", 1))
            if int(inventory.get(item_id, 0)) < quantity:
                raise ValueError(f"not enough {item_id} to apply story effect")
            inventory[item_id] -= quantity
        elif kind == "resource_reward":
            shared.setdefault("pending_rewards", []).append(
                {"type": "resource_reward", "resource": str(effect["resource"]), "amount": amount}
            )
        elif kind in {"unlock_domain", "unlock_difficulty", "add_ghost_slot", "unlock_ability"}:
            shared.setdefault("pending_profile_effects", []).append(dict(effect))
        elif kind == "heal":
            player["hp"] = min(int(player.get("max_hp", 0)), int(player.get("hp", 0)) + amount)
        elif kind == "san_change":
            player["san"] = max(0, min(int(player.get("max_san", 100)), int(player.get("san", 0)) + amount))
        elif kind == "damage":
            player["hp"] = max(0, int(player.get("hp", 0)) - amount)
            if player["hp"] <= 0:
                player["alive"] = False
        elif kind == "increment_counter":
            counter = str(effect["counter"])
            counters = shared.setdefault("counters", {})
            counters[counter] = int(counters.get(counter, 0)) + int(effect.get("amount", 1))
        elif kind == "move_location":
            node_id = str(effect["location_ref"])
            nodes = state.get("route", {}).get("node_ids", [])
            if node_id not in nodes:
                raise ValueError(f"location {node_id!r} is not in the pinned route")
            state["route"]["node_index"] = nodes.index(node_id)
        elif kind == "loyalty_change":
            ghost_ids = player.get("ghost_unit_ids", [])
            units = state.get("ghost_units", [])
            target = next((unit for unit in units if unit.get("unit_id") in ghost_ids and unit.get("owner_profile_id") == profile_id), None)
            if target:
                target["loyalty"] = max(0, min(100, float(target.get("loyalty", 0)) + amount))
        elif kind in {"start_battle", "capture_bonus", "unlock_ability"}:
            if kind == "unlock_ability":
                shared.setdefault("pending_profile_effects", []).append(dict(effect))
            else:
                raise ValueError(f"effect {kind!r} is not valid for a dialogue/location event")
        else:
            raise ValueError(f"unsupported story effect {kind!r}")


def dialogue_view(
    character: Mapping[str, Any], state: Mapping[str, Any], profile_id: str, location_id: str
) -> tuple[str, list[dict[str, Any]]]:
    if character.get("location_ref") != location_id:
        return "这个人物当前不在此地点。", []
    chosen = state.get("shared", {}).get("dialogue_choices", {})
    for node in character.get("dialogue_nodes", []):
        if not conditions_met(node.get("conditions", []), state, profile_id, location_id):
            continue
        node_key = f"{character['id']}:{node['id']}"
        if node_key in chosen:
            return str(node.get("followup", "你们已经谈过这件事。")), []
        choices = [
            choice for choice in node.get("choices", [])
            if conditions_met(choice.get("conditions", []), state, profile_id, location_id)
        ]
        return str(node.get("text", "")), choices
    return "目前没有可继续的对话。", []


def apply_dialogue_choice(
    character: Mapping[str, Any], state: dict[str, Any], profile_id: str, choice_id: str
) -> tuple[bool, str]:
    shared = state.setdefault("shared", {})
    chosen = shared.setdefault("dialogue_choices", {})
    for node in character.get("dialogue_nodes", []):
        node_key = f"{character['id']}:{node['id']}"
        if node_key in chosen:
            continue
        if not conditions_met(node.get("conditions", []), state, profile_id):
            continue
        for choice in node.get("choices", []):
            if choice.get("id") != choice_id:
                continue
            if not conditions_met(choice.get("conditions", []), state, profile_id):
                return False, "当前条件不满足，无法选择该对话选项。"
            apply_run_effects(choice.get("effects", []), state, profile_id)
            chosen[node_key] = choice_id
            return True, str(choice.get("text", choice.get("label", "对话已记录。")))
    return False, "对话选项不存在、条件不满足或已经选择过。"


def trigger_location_events(
    stories: Sequence[Mapping[str, Any]],
    state: dict[str, Any],
    profile_id: str,
    location_id: str,
) -> list[str]:
    shared = state.setdefault("shared", {})
    triggered = shared.setdefault("triggered_events", [])
    messages: list[str] = []
    for story in stories:
        if not conditions_met(story.get("trigger", {}).get("conditions", []), state, profile_id, location_id):
            continue
        for node in story.get("nodes", []):
            event_key = f"{story['id']}:{node['id']}"
            node_location = node.get("location_ref")
            if event_key in triggered or (node_location and node_location != location_id):
                continue
            conditions = [*story.get("trigger", {}).get("conditions", []), *node.get("conditions", [])]
            if not conditions_met(conditions, state, profile_id, location_id):
                continue
            apply_run_effects(node.get("effects", []), state, profile_id)
            if node.get("once", True):
                triggered.append(event_key)
            messages.append(str(node.get("text", "")))
    return messages


def missing_objectives(
    domain: Mapping[str, Any], state: Mapping[str, Any], profile_id: str
) -> list[str]:
    missing: list[str] = []
    current = _current_location_id(state)
    for goal in domain.get("goals", []):
        if goal.get("required", True) and not conditions_met(goal.get("conditions", []), state, profile_id, current):
            missing.append(str(goal.get("id", "unknown")))
    return missing


def _current_location_id(state: Mapping[str, Any]) -> str:
    route = state.get("route", {})
    nodes = route.get("node_ids", [])
    index = int(route.get("node_index", -1))
    return str(nodes[index]) if 0 <= index < len(nodes) else ""
