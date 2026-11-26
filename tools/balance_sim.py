"""Deterministic, isolated battle-encounter balance simulator.

Run from the plugin/repository environment:
    python -m astrbot_plugin_ghost_domain.tools.balance_sim --domain seven_school --samples 500

This reports encounter battle outcomes only, not route completion or chapter-clear rates.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path
from collections import Counter
from collections.abc import Mapping
from typing import Any

from ..content_registry import ContentRegistry
from ..engine.battle import BattleEngine
from ..engine.campaign import encounter_refs_for_difficulty
from ..engine.ghost_control import build_solo_run_state
from ..engine.models import ContentEntry
from ..engine.party import build_party_run_state
from ..engine.randomness import new_rng_state

_OUTCOMES = ("victory", "defeat", "retreated", "timeout")
_MAX_DECISIONS = 500


def _starter_ghost_id(registry: ContentRegistry, domain: ContentEntry) -> str:
    manifest = registry.get_manifest(domain.pack_id, domain.version) or {}
    pool = manifest.get("starter_ghost_pool", [])
    if pool and isinstance(pool[0], dict):
        ghost_id = pool[0].get("ghost_ref")
        if isinstance(ghost_id, str) and registry.get("ghosts", ghost_id, domain.pack_id, domain.version):
            return ghost_id
    entries = sorted(
        registry.list_entries("ghosts", domain.pack_id, domain.version),
        key=lambda entry: entry.data["id"],
    )
    if not entries:
        raise ValueError(f"{domain.pack_id}@{domain.version} has no starter ghost for simulation")
    return str(entries[0].data["id"])


def _sim_profile(profile_id: str, domain: ContentEntry, ghost_id: str) -> dict[str, Any]:
    return {
        "profile_id": profile_id,
        "user_id": profile_id,
        "pack_id": domain.pack_id,
        "pack_version": domain.version,
        "payload": {
            "owned_ghost_ids": [ghost_id],
            "starter_ghost_id": ghost_id,
            "starter_pack_id": domain.pack_id,
            "starter_pack_version": domain.version,
            "owned_ghost_refs": {
                ghost_id: {"pack_id": domain.pack_id, "pack_version": domain.version}
            },
            "active_ghost_ids": [ghost_id],
            "max_ghost_slots": 1,
            "discovered_rules": [],
        },
    }


def _build_state(
    registry: ContentRegistry,
    domain: ContentEntry,
    mode: str,
    difficulty: str,
    seed: str,
) -> dict[str, Any]:
    ghost_id = _starter_ghost_id(registry, domain)
    if mode == "solo":
        profile_id = "balance-solo"
        state = build_solo_run_state(
            _sim_profile(profile_id, domain, ghost_id), domain.data, registry, difficulty
        )
    elif mode == "party":
        party_size = domain.data.get("party_size", {})
        size = int(party_size.get("min", 2)) if isinstance(party_size, dict) else 2
        if not 2 <= size <= 4:
            raise ValueError(f"{domain.data['id']} has unsupported simulated party size {size}")
        profiles = [
            _sim_profile(f"balance-party-{index + 1}", domain, ghost_id)
            for index in range(size)
        ]
        selections = {profile["profile_id"]: ghost_id for profile in profiles}
        state = build_party_run_state(
            profiles,
            domain.data,
            registry,
            str(profiles[0]["profile_id"]),
            selections,
            difficulty,
        )
    else:
        raise ValueError(f"unsupported simulation mode {mode!r}")
    state["rng"] = new_rng_state(seed)
    return state


def _choose_action(
    registry: ContentRegistry, state: Mapping[str, Any]
) -> tuple[str, dict[str, str]]:
    battle = state.get("battle", {})
    actor_id = str(battle.get("current_actor_id") or "")
    if actor_id.startswith("player:"):
        return actor_id.split(":", 1)[1], {"type": "retreat"}

    actor = next(
        (unit for unit in state.get("ghost_units", []) if unit.get("unit_id") == actor_id),
        None,
    )
    if not actor or actor.get("side") != "friendly":
        raise RuntimeError(f"simulator expected a friendly actor, got {actor_id!r}")
    owner_id = str(actor.get("owner_profile_id", ""))
    enemies = [
        unit
        for unit in state.get("ghost_units", [])
        if unit.get("side") == "enemy" and int(unit.get("hp", 0)) > 0
    ]
    if not enemies:
        raise RuntimeError("battle requested a player action after all enemies were defeated")
    target = min(enemies, key=lambda unit: (int(unit.get("hp", 0)), str(unit.get("unit_id", ""))))

    ghost = registry.get(
        "ghosts",
        str(actor.get("ghost_id", "")),
        str(actor.get("ghost_pack_id") or state["pack_id"]),
        str(actor.get("ghost_pack_version") or state["pack_version"]),
    )
    skills = ghost.data.get("skills", []) if ghost else []
    ready_skills = [
        skill
        for skill in skills
        if isinstance(skill, dict)
        and skill.get("type", "attack") == "attack"
        and int(actor.get("cooldowns", {}).get(skill.get("id", ""), 0)) == 0
    ]
    if ready_skills:
        skill = max(ready_skills, key=lambda item: (float(item.get("power", 1)), str(item.get("id", ""))))
        return owner_id, {
            "type": "skill",
            "skill_id": str(skill["id"]),
            "target_id": str(target["unit_id"]),
        }
    return owner_id, {"type": "attack", "target_id": str(target["unit_id"])}


def _outcome(state: Mapping[str, Any]) -> str | None:
    battle = state.get("battle", {})
    status = battle.get("status")
    if status == "victory":
        return "victory"
    if status == "defeat" or state.get("phase") == "dead":
        return "defeat"
    if status == "retreated" or state.get("phase") == "retreated":
        return "retreated"
    return None


def wilson_interval(successes: int, samples: int, z: float = 1.96) -> tuple[float, float]:
    """Return a two-sided Wilson score interval for a Bernoulli rate."""
    if samples < 1 or not 0 <= successes <= samples:
        raise ValueError("Wilson interval requires 0 <= successes <= samples and samples > 0")
    rate = successes / samples
    z2 = z * z
    denominator = 1 + z2 / samples
    center = (rate + z2 / (2 * samples)) / denominator
    margin = z * math.sqrt(rate * (1 - rate) / samples + z2 / (4 * samples * samples)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def simulate_encounter(
    registry: ContentRegistry,
    domain: ContentEntry,
    encounter: Mapping[str, Any],
    mode: str,
    difficulty: str,
    samples: int,
    seed: str,
) -> dict[str, int]:
    if samples < 1:
        raise ValueError("samples must be at least 1")
    refs = encounter_refs_for_difficulty(domain.data, encounter, difficulty, mode)
    if not refs:
        raise ValueError(f"{domain.data['id']} encounter {encounter.get('id')!r} has no refs for {mode}")

    counts = Counter({outcome: 0 for outcome in _OUTCOMES})
    engine = BattleEngine(registry)
    for sample in range(samples):
        trial_seed = f"{seed}:{domain.data['id']}:{domain.version}:{mode}:{difficulty}:{encounter.get('id')}:{sample}"
        state = _build_state(registry, domain, mode, difficulty, trial_seed)
        engine.start_encounter(state, str(encounter.get("id", "balance")), refs)
        outcome = _outcome(state)
        for _decision in range(_MAX_DECISIONS):
            if outcome:
                break
            profile_id, action = _choose_action(registry, state)
            ok, message, _terminal = engine.act(state, profile_id, action)
            if not ok:
                raise RuntimeError(
                    f"balance policy action rejected in {domain.data['id']}@{domain.version} "
                    f"{mode}/{difficulty}/{encounter.get('id')}: {message}"
                )
            outcome = _outcome(state)
        counts[outcome or "timeout"] += 1
    return dict(counts)


def _latest_domains(registry: ContentRegistry) -> list[ContentEntry]:
    latest: dict[str, ContentEntry] = {}
    for entry in registry.entries.values():
        if entry.content_type != "domains":
            continue
        domain_id = str(entry.data.get("id", ""))
        previous = latest.get(domain_id)
        if previous is None or ContentRegistry._version_key(entry.version) > ContentRegistry._version_key(previous.version):
            latest[domain_id] = entry
    return [latest[domain_id] for domain_id in sorted(latest)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default="seven_school", help="domain ID, or 'all' (default: seven_school)")
    parser.add_argument("--samples", type=int, default=200, help="trials per encounter/mode/difficulty")
    parser.add_argument("--seed", default="ghost-domain-balance-v1", help="base deterministic seed")
    args = parser.parse_args(argv)
    if args.samples < 1:
        parser.error("--samples must be at least 1")
    if not args.seed.strip():
        parser.error("--seed must not be empty")

    plugin_root = Path(__file__).resolve().parents[1]
    registry = ContentRegistry(plugin_root / "content" / "packs")
    issues = registry.load()
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1

    domains = _latest_domains(registry)
    if args.domain != "all":
        domains = [entry for entry in domains if entry.data.get("id") == args.domain]
        if not domains:
            parser.error(f"unknown domain {args.domain!r}")

    print("# Battle encounter simulation (not a chapter-clear estimate)")
    print(f"# samples_per_row={args.samples}; base_seed={args.seed}")
    print("# Assumptions: full HP/SAN and mission kit per isolated encounter; first manifest starter ghost per player; attacks focus the enemy with lowest current HP; use highest-power ready attack skill; no observe, items, capture, story choices, or explicit counteraction policy; team size uses configured minimum.")
    print("# Difficulty extra encounters outside the domain encounter list are not sampled separately.")
    writer = csv.writer(sys.stdout)
    writer.writerow(
        [
            "domain_id", "pack_version", "mode", "difficulty", "encounter_id",
            "samples", "victory", "defeat", "retreated", "timeout",
            "battle_win_rate", "win_95ci_low", "win_95ci_high",
        ]
    )

    for domain in domains:
        data = domain.data
        modes = [mode for mode in data.get("supported_modes", ["solo"]) if mode in {"solo", "party"}]
        levels = data.get("difficulty_levels", {})
        difficulties = sorted(levels) if isinstance(levels, dict) and levels else ["normal"]
        encounters = data.get("encounters", [])
        for mode in modes:
            for difficulty in difficulties:
                for encounter in encounters:
                    counts = simulate_encounter(
                        registry, domain, encounter, mode, difficulty, args.samples, args.seed
                    )
                    rate = counts["victory"] / args.samples
                    low, high = wilson_interval(counts["victory"], args.samples)
                    writer.writerow(
                        [
                            data["id"],
                            domain.version,
                            mode,
                            difficulty,
                            encounter.get("id", ""),
                            args.samples,
                            counts["victory"],
                            counts["defeat"],
                            counts["retreated"],
                            counts["timeout"],
                            f"{rate:.4f}",
                            f"{low:.4f}",
                            f"{high:.4f}",
                        ]
                    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
