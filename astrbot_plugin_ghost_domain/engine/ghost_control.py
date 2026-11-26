from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from typing import Any

from .campaign import mission_kit_for_difficulty
from .randomness import new_rng_state, weighted_choice

MAX_GHOST_SLOTS = 2
DEFAULT_PLAYER_HP = 100
DEFAULT_PLAYER_SAN = 100


def recruit_once(profile_payload: dict[str, Any], manifest: Mapping[str, Any]) -> str | None:
    """Assign exactly one weighted starter ghost to a new campaign profile."""
    existing = profile_payload.get("starter_ghost_id")
    if isinstance(existing, str) and existing:
        return existing
    owned = profile_payload.setdefault("owned_ghost_ids", [])
    if owned:
        profile_payload["starter_ghost_id"] = owned[0]
        profile_payload["recruitment_status"] = "recruited"
        return owned[0]

    pool = manifest.get("starter_ghost_pool", [])
    if not isinstance(pool, list):
        raise TypeError("manifest starter_ghost_pool must be a list")
    seed = secrets.token_hex(16)
    rng = new_rng_state(seed)
    choice = weighted_choice(
        rng,
        (
            (str(entry["ghost_ref"]), float(entry.get("weight", 1)))
            for entry in pool
            if isinstance(entry, dict)
            and isinstance(entry.get("ghost_ref"), str)
            and isinstance(entry.get("weight", 1), (int, float))
        ),
    )
    profile_payload["starter_roll_seed"] = seed
    profile_payload["starter_roll_counter"] = rng["counter"]
    profile_payload["starter_ghost_id"] = choice
    profile_payload["owned_ghost_ids"] = [choice]
    profile_payload["recruitment_status"] = "recruited"
    return choice


def capture_probability(
    ghost: Mapping[str, Any],
    current_hp: int,
    max_hp: int,
    item_bonus: float,
    controlled_count: int,
    max_slots: int = MAX_GHOST_SLOTS,
) -> float | None:
    if max_hp <= 0 or current_hp <= 0 or current_hp > max_hp:
        return None
    if controlled_count >= max_slots:
        return None
    ratio = current_hp / max_hp
    threshold = float(ghost.get("capture_threshold", 0))
    if ratio > threshold:
        return None
    difficulty = float(ghost.get("capture_difficulty", 0.5))
    chance = (1 - difficulty) * 0.5 + (threshold - ratio) * 0.5 + item_bonus
    return max(0.0, min(0.95, chance))


def command_refused(loyalty: float, roll: float, threshold: float = 30, chance: float = 0.3) -> bool:
    return loyalty < threshold and roll < chance


def build_unit(
    ghost_data: Mapping[str, Any],
    unit_id: str,
    side: str,
    owner_profile_id: str | None,
) -> dict[str, Any]:
    stats = ghost_data.get("stats", {})
    return {
        "unit_id": unit_id,
        "ghost_id": str(ghost_data["id"]),
        "name": str(ghost_data["name"]),
        "side": side,
        "owner_profile_id": owner_profile_id,
        "hp": int(stats.get("hp", stats.get("max_hp", 1))),
        "max_hp": int(stats.get("hp", stats.get("max_hp", 1))),
        "attack": int(stats.get("attack", 1)),
        "defense": int(stats.get("defense", 0)),
        "speed": int(stats.get("speed", 1)),
        "loyalty": float(ghost_data.get("base_loyalty", 50)) if side == "friendly" else 0,
        "cooldowns": {},
        "counters": {},
        "status": {},
        "captured": False,
        "rebelled": False,
        "recovery": 0,
        "deadlocked": False,
        "recovery_frozen": False,
    }


def _version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(version).lstrip("v").split("."))
    except ValueError:
        return (0,)


def resolve_owned_ghost_entry(
    registry: Any,
    profile_payload: Mapping[str, Any],
    ghost_id: str,
    fallback_pack_id: str,
    fallback_version: str,
) -> Any | None:
    """Resolve an owned ghost from its original pack, migrating legacy profiles deterministically."""
    refs = profile_payload.get("owned_ghost_refs", {})
    ref = refs.get(ghost_id) if isinstance(refs, Mapping) else None
    if ref is not None:
        if not isinstance(ref, Mapping):
            return None
        return registry.get(
            "ghosts", ghost_id, str(ref.get("pack_id", "")), str(ref.get("pack_version", ""))
        )

    if ghost_id == str(profile_payload.get("starter_ghost_id", "")):
        starter_pack = str(profile_payload.get("starter_pack_id", ""))
        starter_version = str(profile_payload.get("starter_pack_version", ""))
        if starter_pack and starter_version:
            entry = registry.get("ghosts", ghost_id, starter_pack, starter_version)
            if entry is not None:
                return entry

    candidates = [
        item
        for item in getattr(registry, "entries", {}).values()
        if item.content_type == "ghosts" and str(item.data.get("id", "")) == ghost_id
    ]
    if not candidates:
        return registry.get("ghosts", ghost_id, fallback_pack_id, fallback_version)
    signatures = {
        hashlib.sha256(json.dumps(item.data, ensure_ascii=False, sort_keys=True).encode("utf-8")).digest()
        for item in candidates
    }
    if len(signatures) > 1:
        return None
    matching_pack = next(
        (item for item in candidates if item.pack_id == fallback_pack_id and item.version == fallback_version),
        None,
    )
    return matching_pack or max(candidates, key=lambda item: (_version_key(item.version), item.pack_id))


def build_solo_run_state(profile: Mapping[str, Any], domain: Mapping[str, Any], registry: Any, difficulty: str = "normal") -> dict[str, Any]:
    profile_id = str(profile["profile_id"])
    pack_id = str(profile["pack_id"])
    version = str(profile["pack_version"])
    payload = profile.get("payload", {})
    owned_ids = [str(item) for item in payload.get("owned_ghost_ids", [])]
    if not owned_ids:
        raise ValueError("player has no permanently owned ghost; complete recruitment first")

    max_slots = max(1, min(6, int(payload.get("max_ghost_slots", MAX_GHOST_SLOTS))))
    raw_active = payload.get("active_ghost_ids")
    if raw_active is None:
        raw_active = [str(payload.get("starter_ghost_id", owned_ids[0]))]
    if not isinstance(raw_active, (list, tuple)) or not raw_active:
        raise ValueError("active ghost formation must contain at least one owned ghost")
    active_ids = [str(item) for item in raw_active]
    if len(active_ids) != len(set(active_ids)):
        raise ValueError("active ghost formation contains duplicate ghosts")
    if len(active_ids) > max_slots:
        raise ValueError(f"active ghost formation exceeds the {max_slots}-slot limit")
    if any(ghost_id not in owned_ids for ghost_id in active_ids):
        raise ValueError("active ghost formation contains a ghost not owned by the profile")

    ghosts: list[dict[str, Any]] = []
    for slot, ghost_id in enumerate(active_ids, start=1):
        entry = resolve_owned_ghost_entry(registry, payload, ghost_id, pack_id, version)
        if entry is None:
            raise ValueError(f"owned ghost {ghost_id!r} has no available pinned content reference")
        unit = build_unit(entry.data, f"friendly:{profile_id}:{slot}", "friendly", profile_id)
        unit["ghost_pack_id"] = entry.pack_id
        unit["ghost_pack_version"] = entry.version
        ghosts.append(unit)

    nodes = domain.get("nodes", [])
    if not nodes:
        raise ValueError("domain requires at least one node")
    inventory: dict[str, int] = {}
    for item in mission_kit_for_difficulty(domain, difficulty):
        if not isinstance(item, dict):
            continue
        item_id = item.get("item_ref")
        quantity = item.get("quantity", 1)
        if isinstance(item_id, str) and isinstance(quantity, int) and quantity > 0:
            if registry.get("items", item_id, pack_id, version) is None:
                raise ValueError(f"mission item {item_id!r} is missing from {pack_id}@{version}")
            inventory[item_id] = inventory.get(item_id, 0) + quantity

    return {
        "schema_version": 2,
        "max_ghost_slots": max_slots,
        "phase": "explore",
        "pack_id": pack_id,
        "pack_version": version,
        "domain_id": str(domain["id"]),
        "difficulty": difficulty,
        "route": {"node_ids": [str(node["id"]) for node in nodes], "node_index": 0},
        "rng": new_rng_state(),
        "players": {
            profile_id: {
                "user_id": str(profile["user_id"]),
                "hp": DEFAULT_PLAYER_HP,
                "max_hp": DEFAULT_PLAYER_HP,
                "san": DEFAULT_PLAYER_SAN,
                "max_san": DEFAULT_PLAYER_SAN,
                "alive": True,
                "recovery": 0,
                "recovery_threshold": 100,
                "max_ghost_slots": max_slots,
                "inventory": inventory,
                "ghost_unit_ids": [ghost["unit_id"] for ghost in ghosts],
                "discovered_rules": list(payload.get("discovered_rules", [])),
                "actions_taken": 0,
                "guard_multiplier": 1.0,
                "status": {},
            }
        },
        "ghost_units": ghosts,
        "shared": {
            "discovered_rules": [],
            "rule_evidence": {},
            "story_flags": {"survivor_helped": False, "survivor_warned": False},
            "environment": dict(nodes[0].get("environment", {})),
        },
        "battle": None,
        "captured_ghost_ids": [],
    }
