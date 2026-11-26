from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .campaign import mission_kit_for_difficulty
from .ghost_control import (
    DEFAULT_PLAYER_HP,
    DEFAULT_PLAYER_SAN,
    MAX_GHOST_SLOTS,
    build_unit,
    resolve_owned_ghost_entry,
)
from .randomness import new_rng_state


def living_profile_ids(state: Mapping[str, Any]) -> list[str]:
    return [
        profile_id
        for profile_id, player in state.get("players", {}).items()
        if bool(player.get("alive", int(player.get("hp", 0)) > 0)) and int(player.get("hp", 0)) > 0
    ]


def current_explorer(state: Mapping[str, Any]) -> str | None:
    order = state.get("party", {}).get("exploration_order", [])
    if not order:
        return None
    cursor = int(state.get("party", {}).get("exploration_cursor", 0)) % len(order)
    players = state.get("players", {})
    for offset in range(len(order)):
        profile_id = order[(cursor + offset) % len(order)]
        player = players.get(profile_id, {})
        if bool(player.get("alive", int(player.get("hp", 0)) > 0)) and int(player.get("hp", 0)) > 0:
            return profile_id
    return None


def complete_exploration_action(state: dict[str, Any], profile_id: str) -> bool:
    if current_explorer(state) != profile_id:
        return False
    order = state["party"]["exploration_order"]
    player = state["players"][profile_id]
    player["actions_taken"] = int(player.get("actions_taken", 0)) + 1
    state["party"]["exploration_cursor"] = (int(state["party"].get("exploration_cursor", 0)) + 1) % len(order)
    return True


def record_effective_action(state: dict[str, Any], profile_id: str) -> None:
    player = state.get("players", {}).get(profile_id)
    if player is not None:
        player["actions_taken"] = int(player.get("actions_taken", 0)) + 1


def party_defeated(state: Mapping[str, Any]) -> bool:
    if not living_profile_ids(state):
        return True
    if not state.get("party"):
        return False
    alive_ghosts = [
        unit
        for unit in state.get("ghost_units", [])
        if unit.get("side") == "friendly" and int(unit.get("hp", 0)) > 0 and not unit.get("rebelled")
        and int(state.get("players", {}).get(unit.get("owner_profile_id"), {}).get("hp", 0)) > 0
        and state.get("players", {}).get(unit.get("owner_profile_id"), {}).get("alive", True)
    ]
    return not alive_ghosts and bool(state["party"].get("desperation_used"))


def eligible_settlement_profiles(
    state: Mapping[str, Any], minimum_actions: int = 2
) -> list[str]:
    return [
        profile_id
        for profile_id, player in state.get("players", {}).items()
        if int(player.get("actions_taken", 0)) >= minimum_actions
    ]


def persistable_captures(state: Mapping[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    players = state.get("players", {})
    for profile_id, ghost_ids in state.get("captured_by_profile", {}).items():
        player = players.get(profile_id, {})
        if bool(player.get("alive", int(player.get("hp", 0)) > 0)) and int(player.get("hp", 0)) > 0:
            result[profile_id] = list(dict.fromkeys(ghost_ids))
    return result


def build_party_run_state(
    profiles: list[Mapping[str, Any]],
    domain: Mapping[str, Any],
    registry: Any,
    leader_profile_id: str,
    selected_ghost_ids: Mapping[str, str] | None = None,
    difficulty: str = "normal",
) -> dict[str, Any]:
    if not 2 <= len(profiles) <= 4:
        raise ValueError("party run requires 2 to 4 locked profiles")
    if leader_profile_id not in {str(profile["profile_id"]) for profile in profiles}:
        raise ValueError("party leader must be a locked member")
    first = profiles[0]
    pack_id, version = str(first["pack_id"]), str(first["pack_version"])
    domain_entry = registry.get("domains", str(domain["id"]), pack_id, version)
    if domain_entry is None:
        raise ValueError("domain is not in the pinned party content version")
    nodes = domain_entry.data.get("nodes", [])
    if not nodes:
        raise ValueError("party domain needs at least one route node")
    players: dict[str, dict[str, Any]] = {}
    ghost_units: list[dict[str, Any]] = []
    order: list[str] = []
    for profile in profiles:
        profile_id = str(profile["profile_id"])
        if (str(profile["pack_id"]), str(profile["pack_version"])) != (pack_id, version):
            raise ValueError("all party members must use the same immutable content version")
        payload = profile.get("payload", {})
        owned = payload.get("owned_ghost_ids", [])
        if not owned:
            raise ValueError(f"member {profile_id} has no recruited ghost")
        raw_selected = (selected_ghost_ids or {}).get(profile_id)
        if raw_selected is None:
            raw_selected = payload.get("active_ghost_ids")
        if raw_selected is None:
            raw_selected = [str(payload.get("starter_ghost_id", owned[0]))]
        selected = [str(raw_selected)] if isinstance(raw_selected, str) else [str(item) for item in raw_selected]
        selected = list(dict.fromkeys(selected))
        slots = max(1, min(6, int(payload.get("max_ghost_slots", MAX_GHOST_SLOTS))))
        if not selected:
            raise ValueError(f"member {profile_id} must select at least one ghost")
        if len(selected) > slots:
            raise ValueError(f"member {profile_id} formation exceeds the {slots}-slot limit")
        roster_unit_ids: list[str] = []
        for ghost_id in selected:
            if ghost_id not in owned:
                raise ValueError(f"member {profile_id} does not own selected ghost {ghost_id!r}")
            ghost_entry = resolve_owned_ghost_entry(registry, payload, ghost_id, pack_id, version)
            if ghost_entry is None:
                raise ValueError(f"member owned ghost {ghost_id!r} has no available content reference")
            unit_id = f"friendly:{profile_id}:{len(roster_unit_ids) + 1}"
            ghost = build_unit(ghost_entry.data, unit_id, "friendly", profile_id)
            ghost["ghost_pack_id"] = ghost_entry.pack_id
            ghost["ghost_pack_version"] = ghost_entry.version
            ghost_units.append(ghost)
            roster_unit_ids.append(unit_id)
        inventory: dict[str, int] = {}
        for item in mission_kit_for_difficulty(domain_entry.data, difficulty):
            item_id = item.get("item_ref")
            quantity = item.get("quantity", 1)
            if not isinstance(item_id, str) or not isinstance(quantity, int) or quantity <= 0:
                continue
            if registry.get("items", item_id, pack_id, version) is None:
                raise ValueError(f"mission item {item_id!r} is missing from pinned content")
            inventory[item_id] = inventory.get(item_id, 0) + quantity
        players[profile_id] = {
            "user_id": str(profile.get("user_id", profile_id)),
            "hp": DEFAULT_PLAYER_HP,
            "max_hp": DEFAULT_PLAYER_HP,
            "san": DEFAULT_PLAYER_SAN,
            "max_san": DEFAULT_PLAYER_SAN,
            "alive": True,
            "recovery": 0,
            "recovery_threshold": 100,
            "max_ghost_slots": max(1, min(6, int(payload.get("max_ghost_slots", MAX_GHOST_SLOTS)))),
            "inventory": inventory,
            "ghost_unit_ids": roster_unit_ids,
            "discovered_rules": list(payload.get("discovered_rules", [])),
            "actions_taken": 0,
            "guard_multiplier": 1.0,
            "status": {},
        }
        order.append(profile_id)
    return {
        "schema_version": 2,
        "max_ghost_slots": max(
            int(profile.get("payload", {}).get("max_ghost_slots", MAX_GHOST_SLOTS))
            for profile in profiles
        ),
        "mode": "party",
        "phase": "explore",
        "pack_id": pack_id,
        "pack_version": version,
        "domain_id": str(domain_entry.data["id"]),
        "difficulty": difficulty,
        "route": {"node_ids": [str(node["id"]) for node in nodes], "node_index": 0},
        "rng": new_rng_state(),
        "players": players,
        "ghost_units": ghost_units,
        "shared": {
            "discovered_rules": [],
            "rule_evidence": {},
            "story_flags": {"survivor_helped": False, "survivor_warned": False},
            "environment": dict(nodes[0].get("environment", {})),
        },
        "party": {
            "leader_profile_id": leader_profile_id,
            "exploration_order": order,
            "exploration_cursor": 0,
            "desperation_used": False,
        },
        "battle": None,
        "captured_ghost_ids": [],
        "captured_by_profile": {},
        "settlement_id": None,
    }
