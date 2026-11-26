from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .progression import apply_completion
from .content_contracts import (
    SUPPORTED_CAMPAIGN_REWARD_TYPES,
    SUPPORTED_PROFILE_EFFECT_TYPES,
)


def difficulty_is_unlocked(
    profile_payload: Mapping[str, Any], domain: Mapping[str, Any], difficulty: str
) -> bool:
    levels = domain.get("difficulty_levels", {})
    level = levels.get(difficulty)
    if not isinstance(level, dict):
        return difficulty == "normal" and not levels
    if level.get("unlocked_by_default", False):
        return True
    unlocked = profile_payload.get("unlocked_difficulties", {}).get(domain.get("id"), ["normal"])
    return difficulty in unlocked



def difficulty_config(domain: Mapping[str, Any], difficulty: str) -> dict[str, Any]:
    config = domain.get("difficulty_variants", {}).get(difficulty, {})
    return config if isinstance(config, dict) else {}


def mission_kit_for_difficulty(domain: Mapping[str, Any], difficulty: str) -> list[dict[str, Any]]:
    variant = difficulty_config(domain, difficulty)
    removed = set(variant.get("mission_kit_remove", []))
    kit = [dict(item) for item in domain.get("mission_kit", []) if item.get("item_ref") not in removed]
    for item in variant.get("mission_kit_add", []):
        kit.append(dict(item))
    return kit


def encounter_refs_for_difficulty(
    domain: Mapping[str, Any], encounter: Mapping[str, Any], difficulty: str, mode: str
) -> list[dict[str, Any]]:
    variant = difficulty_config(domain, difficulty)
    encounter_override = variant.get("encounters", {}).get(encounter.get("id"), {})
    mode_variant = encounter_override.get("mode_variants", {}).get(mode, {})
    if not mode_variant:
        mode_variant = encounter.get("mode_variants", {}).get(mode, {})
    refs = mode_variant.get("enemy_refs", encounter_override.get("enemy_refs", encounter.get("enemy_refs", [])))
    overrides = variant.get("enemy_behavior_overrides", {})
    resolved: list[dict[str, Any]] = []
    for reference in refs:
        item = dict(reference)
        behavior = dict(item.get("behavior_overrides", {}))
        behavior.update(overrides.get(item.get("ghost_ref"), {}))
        if behavior:
            item["behavior_overrides"] = behavior
        resolved.append(item)
    return resolved


def extra_encounters_for_difficulty(domain: Mapping[str, Any], difficulty: str) -> list[dict[str, Any]]:
    return [dict(item) for item in difficulty_config(domain, difficulty).get("extra_encounters", [])]

def settle_profile(
    profile_payload: dict[str, Any],
    state: Mapping[str, Any],
    profile_id: str,
    domain: Mapping[str, Any],
    outcome: str,
    settlement_id: str,
    eligible: bool,
) -> dict[str, Any]:
    settled = profile_payload.setdefault("settled_runs", [])
    if settlement_id in settled:
        return {"duplicate": True, "completed": outcome == "completed", "first_clear": False, "rewards": []}

    domain_id = str(domain.get("id", ""))
    difficulty = str(state.get("difficulty") or "normal")
    completed_difficulties = profile_payload.get("completed_difficulties")
    if not isinstance(completed_difficulties, dict):
        completed_difficulties = {}
        profile_payload["completed_difficulties"] = completed_difficulties
    for completed_domain in profile_payload.get("completed_domains", []):
        completed_difficulties.setdefault(str(completed_domain), ["normal"])

    difficulty_clears = completed_difficulties.get(domain_id)
    if not isinstance(difficulty_clears, list):
        difficulty_clears = []
        completed_difficulties[domain_id] = difficulty_clears
    first_clear = outcome == "completed" and eligible and difficulty not in difficulty_clears
    if first_clear:
        difficulty_clears.append(difficulty)

    player = state.get("players", {}).get(profile_id, {})
    alive = int(player.get("hp", 0)) > 0 and player.get("alive", True)
    if eligible:
        journal = profile_payload.setdefault("discovered_rules", [])
        for rule_id in player.get("discovered_rules", []):
            if rule_id not in journal:
                journal.append(rule_id)
        for rule_id in state.get("shared", {}).get("discovered_rules", []):
            if rule_id not in journal:
                journal.append(rule_id)
        clues = profile_payload.setdefault("legacy_clues", [])
        for flag, value in state.get("shared", {}).get("story_flags", {}).items():
            if value and str(flag).startswith("clue_") and flag not in clues:
                clues.append(str(flag))

    new_captures: list[str] = []
    if outcome in {"completed", "retreated"} and alive and eligible:
        new_captures = list(state.get("captured_by_profile", {}).get(profile_id, []))
        if not new_captures and not state.get("party"):
            new_captures = list(state.get("captured_ghost_ids", []))
        owned = profile_payload.setdefault("owned_ghost_ids", [])
        refs = profile_payload.get("owned_ghost_refs")
        if not isinstance(refs, dict):
            refs = {}
            profile_payload["owned_ghost_refs"] = refs
        source_ref = {
            "pack_id": str(state.get("pack_id", "")),
            "pack_version": str(state.get("pack_version", "")),
        }
        for ghost_id in new_captures:
            if ghost_id not in owned:
                owned.append(ghost_id)
            if source_ref["pack_id"] and source_ref["pack_version"]:
                refs.setdefault(str(ghost_id), source_ref)

    rewards: list[dict[str, Any]] = []
    levels = domain.get("difficulty_levels", {})
    level = levels.get(difficulty, {})
    if not levels and difficulty == "normal":
        level = {"rewards": [{"type": "resource_reward", "resource": "soul_shards", "amount": 1}], "unlocks": ["hard"]}
    if first_clear:
        rewards.extend(level.get("rewards", []))
        rewards.extend(
            {**reward, "type": "resource_reward"} if "type" not in reward else reward
            for reward in state.get("shared", {}).get("pending_rewards", [])
        )
        resources = profile_payload.setdefault("campaign_resources", {})
        unlocked = profile_payload.setdefault("unlocked_difficulties", {}).setdefault(domain["id"], ["normal"])
        for reward in rewards:
            reward_type = reward.get("type")
            if not isinstance(reward_type, str) or reward_type not in SUPPORTED_CAMPAIGN_REWARD_TYPES:
                raise ValueError(f"unsupported campaign reward {reward_type!r}")
            if reward_type == "resource_reward":
                resource = str(reward.get("resource", "soul_shards"))
                resources[resource] = int(resources.get(resource, 0)) + int(reward.get("amount", 0))
            elif reward_type == "unlock_difficulty":
                next_difficulty = str(reward.get("difficulty", ""))
                if next_difficulty and next_difficulty not in unlocked:
                    unlocked.append(next_difficulty)
        for next_difficulty in level.get("unlocks", []):
            if next_difficulty not in unlocked:
                unlocked.append(next_difficulty)

    for effect in state.get("shared", {}).get("pending_profile_effects", []):
        kind = effect.get("type")
        if not isinstance(kind, str) or kind not in SUPPORTED_PROFILE_EFFECT_TYPES:
            raise ValueError(f"unsupported profile effect {kind!r}")
        if not first_clear:
            continue
        if kind == "unlock_difficulty":
            unlocked = profile_payload.setdefault("unlocked_difficulties", {}).setdefault(domain["id"], ["normal"])
            value = str(effect.get("difficulty", ""))
            if value and value not in unlocked:
                unlocked.append(value)
        elif kind == "unlock_domain":
            values = profile_payload.setdefault("unlocked_domains", [])
            value = str(effect.get("domain_ref", ""))
            if value and value not in values:
                values.append(value)
        elif kind == "add_ghost_slot":
            profile_payload["max_ghost_slots"] = min(6, max(1, int(profile_payload.get("max_ghost_slots", 2)) + int(effect.get("amount", 1))))
        elif kind == "unlock_ability":
            values = profile_payload.setdefault("unlocked_abilities", [])
            value = str(effect.get("ability_ref", ""))
            if value and value not in values:
                values.append(value)

    profile_payload["deadlock_pairs"] = max(
        int(profile_payload.get("deadlock_pairs", 0)), int(state.get("deadlock_pairs", 0))
    )
    owned = profile_payload.setdefault("owned_ghost_ids", [])
    if owned:
        from .recovery import lifespan_days

        profile_payload["lifespan_days"] = lifespan_days(
            len(owned), balance=0.5, deadlocked_pairs=int(profile_payload.get("deadlock_pairs", 0))
        )
    settled.append(settlement_id)
    progress = apply_completion(profile_payload, domain, outcome if eligible else "ineligible")
    profile_payload["last_run_result"] = {
        "settlement_id": settlement_id,
        "outcome": outcome,
        "difficulty": difficulty,
        "eligible": eligible,
        "first_clear": first_clear,
        "captured_ghost_ids": new_captures,
        "rewards": list(rewards) if first_clear else [],
        "unlocked_domains": progress["unlocked_domains"],
    }
    return {
        "duplicate": False,
        "completed": outcome == "completed",
        "eligible": eligible,
        "first_clear": first_clear,
        "rewards": rewards,
        "captures": new_captures,
        "unlocked_domains": progress["unlocked_domains"],
        "ending": progress["ending"],
    }
