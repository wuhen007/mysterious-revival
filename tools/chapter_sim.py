"""Run deterministic scripted full-chapter simulations through the game services.

Usage:
    python -m astrbot_plugin_ghost_domain.tools.chapter_sim --domain seven_school --samples 50 --mode both

Unlike balance_sim, results include exploration, battles, objectives and extraction.
The built-in policy is a scripted reference player, not a model of human behavior.
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from ..content_registry import ContentRegistry
from ..engine.models import Identity
from ..engine.party import current_explorer
from ..engine.randomness import new_rng_state
from ..engine.story import dialogue_view, missing_objectives
from ..handlers.commands import CommandHandler
from ..modes.party_run import PartyRunService
from ..modes.solo_run import SoloRunService
from ..storage.repository import GameRepository
from .balance_sim import wilson_interval

_OUTCOMES = (
    "completed",
    "dead",
    "retreated",
    "action_limit",
    "objective_blocked",
    "blocked",
    "timeout",
)
_MAX_STEPS = 160
_MAX_DAY_ROLLOVERS = 30


def _starter_id(registry: ContentRegistry, domain: Any) -> str:
    manifest = registry.get_manifest(domain.pack_id, domain.version) or {}
    pool = manifest.get("starter_ghost_pool", [])
    if pool and isinstance(pool[0], dict):
        ghost_id = pool[0].get("ghost_ref")
        if isinstance(ghost_id, str) and registry.get("ghosts", ghost_id, domain.pack_id, domain.version):
            return ghost_id
    ghosts = sorted(registry.list_entries("ghosts", domain.pack_id, domain.version), key=lambda item: item.data["id"])
    if not ghosts:
        raise ValueError(f"{domain.pack_id}@{domain.version} has no starter ghost")
    return str(ghosts[0].data["id"])


class _ChapterTrial:
    def __init__(
        self,
        registry: ContentRegistry,
        domain: Any,
        mode: str,
        difficulty: str,
        seed: str,
        sample: int,
        database_path: Path,
    ) -> None:
        self.registry = registry
        self.domain = domain
        self.mode = mode
        self.difficulty = difficulty
        self.repository = GameRepository(database_path)
        self.solo = SoloRunService(self.repository, registry)
        self.party = PartyRunService(self.repository, registry)
        self.handler = CommandHandler(self.repository, registry, self.solo, self.party)
        self.sequence = 0
        self.identities: dict[str, Identity] = {}
        self.profile_ids: dict[str, str] = {}
        self.investigated_nodes: set[str] = set()
        self.domain_data = domain.data
        self.ghost_id = _starter_id(registry, domain)
        self.seed = f"{seed}:{domain.data['id']}:{domain.version}:{mode}:{difficulty}:{sample}"
        self.run_id = ""
        self._start()

    def _identity(self, role: str) -> Identity:
        base = self.identities[role]
        self.sequence += 1
        return Identity(
            base.adapter_id,
            base.adapter_name,
            base.user_id,
            base.group_id,
            f"chapter-sim-{self.sequence}",
        )

    def _prepare_profile(self, role: str) -> None:
        self.handler.recruit(self._identity(role))
        base = self.identities[role]
        profile = self.repository.get_profile(base)
        if not profile:
            raise RuntimeError("simulator could not create a profile")
        payload = dict(profile["payload"])
        payload.update(
            {
                "owned_ghost_ids": [self.ghost_id],
                "starter_ghost_id": self.ghost_id,
                "starter_pack_id": self.domain.pack_id,
                "starter_pack_version": self.domain.version,
                "owned_ghost_refs": {
                    self.ghost_id: {
                        "pack_id": self.domain.pack_id,
                        "pack_version": self.domain.version,
                    }
                },
                "active_ghost_ids": [self.ghost_id],
                "max_ghost_slots": 1,
                "unlocked_domains": list(
                    set(payload.get("unlocked_domains", [])) | {str(self.domain_data["id"])}
                ),
                "unlocked_difficulties": {
                    **payload.get("unlocked_difficulties", {}),
                    str(self.domain_data["id"]): list(
                        self.domain_data.get("difficulty_levels", {"normal": {}}).keys()
                    ),
                },
            }
        )
        with self.repository.transaction() as connection:
            self.repository.save_profile_payload_in(connection, profile["profile_id"], payload)
        self.profile_ids[role] = str(profile["profile_id"])

    def _start(self) -> None:
        group_id = f"chapter-sim-{self.mode}-{self.difficulty}-{self.sequence}-{self.seed[-8:]}"
        roles = ("leader", "member") if self.mode == "party" else ("leader",)
        for role in roles:
            self.identities[role] = Identity(
                "aiocqhttp",
                "aiocqhttp",
                f"{self.seed[-8:]}-{role}",
                group_id if self.mode == "party" else "",
            )
            self._prepare_profile(role)

        difficulty_name = {"normal": "普通", "hard": "困难"}.get(self.difficulty, self.difficulty)
        mode_name = "团队" if self.mode == "party" else "单人"
        request = f"{self.domain_data.get('name', self.domain_data['id'])} {mode_name} {difficulty_name}"
        response = self.handler.open_run(self._identity("leader"), request)
        run = self.repository.get_active_run(self.identities["leader"])
        if not run:
            raise RuntimeError(f"simulator could not start chapter: {response.text}")
        self.run_id = str(run["run_id"])

        if self.mode == "party":
            invite_code = str(run["invite_code"])
            joined = self.handler.join(self._identity("member"), invite_code)
            if "已加入" not in joined.text:
                raise RuntimeError(f"simulator member could not join: {joined.text}")
            for role in roles:
                ready = self.handler.ready(self._identity(role))
                if "准备" not in ready.text and "已准备" not in ready.text:
                    raise RuntimeError(f"simulator member could not ready: {ready.text}")
            started = self.handler.start_party(self._identity("leader"))
            run = self.repository.get_active_run(self.identities["leader"])
            if not run or run["status"] != "active":
                raise RuntimeError(f"simulator party could not start: {started.text}")

        run = self.repository.get_active_run(self.identities["leader"])
        if not run:
            raise RuntimeError("simulator run disappeared during setup")
        state = run["state"]
        state["rng"] = new_rng_state(self.seed)
        with self.repository.transaction() as connection:
            self.repository.save_run_state_in(connection, self.run_id, run["version"], state)

    def _advance_simulated_day(self) -> bool:
        if self.simulated_day_rollovers >= _MAX_DAY_ROLLOVERS:
            return False
        with self.repository.transaction() as connection:
            for role in self.profile_ids:
                profile = self.repository.profile_in(connection, self.identities[role])
                if not profile:
                    raise RuntimeError("simulator profile disappeared during daily reset")
                payload = dict(profile["payload"])
                payload["daily"] = {"date": "", "used": 0}
                self.repository.save_profile_payload_in(connection, profile["profile_id"], payload)
        self.simulated_day_rollovers += 1
        return True

    def _active_run(self) -> dict[str, Any] | None:
        return self.repository.run_snapshot(self.run_id)

    def _identity_for_profile(self, profile_id: str) -> Identity:
        for role, found in self.profile_ids.items():
            if found == profile_id:
                return self.identities[role]
        raise RuntimeError(f"no simulator identity for profile {profile_id!r}")

    def _submit(self, profile_id: str, action: dict[str, Any]) -> tuple[bool, str]:
        before = self._active_run()
        if not before:
            return False, "run disappeared"
        identity = self._identity(self._role_for_profile(profile_id))
        service = self.party if self.mode == "party" else self.solo
        response = service.perform_text_action(identity, action)
        after = self._active_run()
        if after and after["version"] > before["version"]:
            return True, response.text
        return False, response.text

    def _role_for_profile(self, profile_id: str) -> str:
        for role, found in self.profile_ids.items():
            if found == profile_id:
                return role
        raise RuntimeError(f"unknown simulator profile {profile_id!r}")

    def _exploration_action(self, state: dict[str, Any]) -> dict[str, Any]:
        if self.mode == "party":
            explorer_id = current_explorer(state)
            if not explorer_id:
                raise RuntimeError("party has no current explorer")
        else:
            explorer_id = self.profile_ids["leader"]
        route = state.get("route", {})
        node_ids = route.get("node_ids", [])
        index = int(route.get("node_index", -1))
        if not 0 <= index < len(node_ids):
            return {"type": "forward"}
        location = str(node_ids[index])

        for character in self.registry.list_entries("characters", self.domain.pack_id, self.domain.version):
            text, choices = dialogue_view(character.data, state, explorer_id, location)
            del text
            if choices:
                choice = choices[0]
                return {
                    "type": "story_choice",
                    "character_id": str(character.data["id"]),
                    "choice_id": str(choice["id"]),
                }

        node = next(
            (item for item in self.domain_data.get("nodes", []) if str(item.get("id")) == location),
            {},
        )
        clues = node.get("clues", [])
        if clues and location not in self.investigated_nodes:
            self.investigated_nodes.add(location)
            return {"type": "investigate"}
        return {"type": "forward"}

    def _battle_action(self, state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        battle = state.get("battle", {})
        actor_id = str(battle.get("current_actor_id") or "")
        if actor_id.startswith("player:"):
            profile_id = actor_id.split(":", 1)[1]
            player = state.get("players", {}).get(profile_id, {})
            if int(player.get("hp", 0)) < int(player.get("max_hp", 0)):
                for item_id, quantity in player.get("inventory", {}).items():
                    if quantity <= 0:
                        continue
                    item = self.registry.get("items", str(item_id), state["pack_id"], state["pack_version"])
                    if item and any(effect.get("type") == "heal" for effect in item.data.get("effects", [])):
                        return profile_id, {"type": "item", "item_id": str(item_id), "target_id": "player"}
            return profile_id, {"type": "retreat"}
        actor = next(
            (unit for unit in state.get("ghost_units", []) if unit.get("unit_id") == actor_id),
            None,
        )
        if not actor or actor.get("side") != "friendly":
            raise RuntimeError(f"simulator expected a friendly battle actor, got {actor_id!r}")
        profile_id = str(actor.get("owner_profile_id", ""))
        player = state.get("players", {}).get(profile_id, {})
        slot = next(
            (index for index, unit_id in enumerate(player.get("ghost_unit_ids", []), start=1) if unit_id == actor_id),
            1,
        )
        enemies = [
            unit
            for unit in state.get("ghost_units", [])
            if unit.get("side") == "enemy" and int(unit.get("hp", 0)) > 0
        ]
        if not enemies:
            raise RuntimeError("simulator requested an action after all enemies were defeated")

        required_rules = {
            str(condition.get("rule_ref"))
            for goal in self.domain_data.get("goals", [])
            for condition in goal.get("conditions", [])
            if condition.get("type") in {"rule_discovered", "rule_countered"}
        }
        shared = state.get("shared", {})
        discovered = set(shared.get("discovered_rules", []))
        countered = shared.get("rule_counteractions", {})
        for enemy in enemies:
            ghost = self.registry.get(
                "ghosts", str(enemy.get("ghost_id", "")),
                str(enemy.get("ghost_pack_id") or state["pack_id"]),
                str(enemy.get("ghost_pack_version") or state["pack_version"]),
            )
            if not ghost:
                continue
            behavior = ghost.data.get("behavior", {})
            for rule in ghost.data.get("rules", []):
                rule_id = str(rule.get("id", ""))
                if rule_id not in required_rules:
                    continue
                counteractions = set(rule.get("counteractions", []))
                goal_conditions = [
                    condition
                    for goal in self.domain_data.get("goals", [])
                    for condition in goal.get("conditions", [])
                ]
                counter_by_story_flag = any(
                    condition.get("type") == "story_flag_matches"
                    and condition.get("flag") == rule.get("story_flag_on_counteraction")
                    and condition.get("value") is True
                    for condition in goal_conditions
                )
                counter_by_rule = any(
                    condition.get("type") == "rule_countered"
                    and str(condition.get("rule_ref", "")) == rule_id
                    for condition in goal_conditions
                )
                if (counter_by_rule or counter_by_story_flag) and not countered.get(rule_id):
                    counter_name = str(behavior.get("counter_name", "knocks"))
                    count = int(enemy.get("counters", {}).get(counter_name, 0))
                    threshold = max(1, int(behavior.get("knocks_before_attack", 3)))
                    counter_is_due = counter_by_rule or count >= max(1, threshold - 2)
                    if "damage_resets_counter" in counteractions and count > 0 and counter_is_due:
                        counter_target = str(enemy.get("unit_id", ""))
                        ghost_data = self.registry.get(
                            "ghosts", str(actor.get("ghost_id", "")),
                            str(actor.get("ghost_pack_id") or state["pack_id"]),
                            str(actor.get("ghost_pack_version") or state["pack_version"]),
                        )
                        ready_skills = [
                            skill for skill in (ghost_data.data.get("skills", []) if ghost_data else [])
                            if skill.get("type", "attack") == "attack"
                            and int(actor.get("cooldowns", {}).get(skill.get("id", ""), 0)) == 0
                        ]
                        if ready_skills:
                            skill = max(ready_skills, key=lambda item: (float(item.get("power", 1)), str(item.get("id", ""))))
                            return profile_id, {
                                "type": "skill", "slot": slot,
                                "skill_id": str(skill["id"]), "target_id": counter_target,
                            }
                        return profile_id, {"type": "attack", "slot": slot, "target_id": counter_target}
                    if "set_distance_far" in counteractions and state.get("shared", {}).get("environment", {}).get("distance") != "far":
                        return profile_id, {"type": "escort_retreat", "slot": slot}
                    if "illuminate" in counteractions and state.get("shared", {}).get("environment", {}).get("light") == "dark":
                        candle = next(
                            (item_id for item_id, quantity in player.get("inventory", {}).items()
                             if quantity > 0 and any(effect.get("type") == "illuminate" for effect in (self.registry.get("items", item_id, self.domain.pack_id, self.domain.version) or type("Entry", (), {"data": {}})()).data.get("effects", []))),
                            None,
                        )
                        if candle:
                            return profile_id, {"type": "item", "item_id": candle, "target_id": str(enemy.get("unit_id", ""))}
                if rule_id not in discovered:
                    evidence = shared.get("rule_evidence", {}).get(f"{enemy['unit_id']}:{rule_id}", {})
                    if not evidence.get("discovered"):
                        return profile_id, {
                            "type": "observe", "slot": slot,
                            "target_id": str(enemy.get("unit_id", "")), "rule_id": rule_id,
                        }

        target = min(enemies, key=lambda unit: (int(unit.get("hp", 0)), str(unit.get("unit_id", ""))))
        unit_ghost = self.registry.get(
            "ghosts", str(actor.get("ghost_id", "")),
            str(actor.get("ghost_pack_id") or state["pack_id"]),
            str(actor.get("ghost_pack_version") or state["pack_version"]),
        )
        skills = unit_ghost.data.get("skills", []) if unit_ghost else []
        ready = [
            skill for skill in skills
            if skill.get("type", "attack") == "attack"
            and int(actor.get("cooldowns", {}).get(skill.get("id", ""), 0)) == 0
        ]
        if ready:
            skill = max(ready, key=lambda item: (float(item.get("power", 1)), str(item.get("id", ""))))
            return profile_id, {
                "type": "skill", "slot": slot, "skill_id": str(skill["id"]),
                "target_id": str(target.get("unit_id", "")),
            }
        return profile_id, {"type": "attack", "slot": slot, "target_id": str(target.get("unit_id", ""))}

    def run(self) -> str:
        for _step in range(_MAX_STEPS):
            snapshot = self._active_run()
            if not snapshot:
                return "blocked"
            state = snapshot["state"]
            if snapshot["status"] not in {"active", "lobby"}:
                phase = state.get("phase")
                return {
                    "extracted": "completed",
                    "dead": "dead",
                    "retreated": "retreated",
                }.get(str(phase), "blocked")

            phase = str(state.get("phase", ""))
            if phase == "battle":
                profile_id, action = self._battle_action(state)
            elif phase == "extraction":
                missing = missing_objectives(
                    self.domain_data,
                    state,
                    str(state.get("party", {}).get("leader_profile_id") or self.profile_ids["leader"]),
                )
                if missing:
                    return "objective_blocked"
                profile_id = self.profile_ids["leader"]
                action = {"type": "extract"}
            elif phase == "explore":
                profile_id = current_explorer(state) if self.mode == "party" else self.profile_ids["leader"]
                if not profile_id:
                    return "blocked"
                action = self._exploration_action(state)
            elif phase == "dead":
                return "dead"
            elif phase == "retreated":
                return "retreated"
            else:
                return "blocked"

            ok, message = self._submit(str(profile_id), action)
            if not ok:
                if "行动已用尽" in message or "今日行动" in message and "用尽" in message:
                    if self._advance_simulated_day():
                        continue
                    return "action_limit"
                if phase == "extraction" and "撤离条件" in message:
                    return "objective_blocked"
                return "blocked"
        return "timeout"


def _prepare_profile(handler: CommandHandler, repository: GameRepository, identity: Identity, domain: Any, ghost_id: str) -> str:
    handler.recruit(identity)
    profile = repository.get_profile(identity)
    if not profile:
        raise RuntimeError("chapter simulator could not create a profile")
    payload = dict(profile["payload"])
    required_clues = int(domain.data.get("unlock", {}).get("requires_clues", 0) or 0)
    legacy_clues = list(payload.get("legacy_clues", []))
    legacy_clues.extend(
        "chapter-sim-entry-prerequisite"
        for _ in range(max(0, required_clues - len(legacy_clues)))
    )
    payload.update(
        {
            "legacy_clues": legacy_clues,
            "owned_ghost_ids": [ghost_id],
            "starter_ghost_id": ghost_id,
            "starter_pack_id": domain.pack_id,
            "starter_pack_version": domain.version,
            "owned_ghost_refs": {ghost_id: {"pack_id": domain.pack_id, "pack_version": domain.version}},
            "active_ghost_ids": [ghost_id],
            "max_ghost_slots": 1,
            "unlocked_domains": list(set(payload.get("unlocked_domains", [])) | {str(domain.data["id"])}),
            "unlocked_difficulties": {
                **payload.get("unlocked_difficulties", {}),
                str(domain.data["id"]): list(domain.data.get("difficulty_levels", {"normal": {}}).keys()),
            },
        }
    )
    with repository.transaction() as connection:
        repository.save_profile_payload_in(connection, profile["profile_id"], payload)
    return str(profile["profile_id"])


def simulate_chapter(
    registry: ContentRegistry,
    domain: Any,
    mode: str,
    difficulty: str,
    samples: int,
    seed: str,
) -> dict[str, int]:
    if samples < 1:
        raise ValueError("samples must be at least 1")
    if mode not in {"solo", "party"}:
        raise ValueError(f"unsupported chapter mode {mode!r}")
    if mode not in domain.data.get("supported_modes", []):
        raise ValueError(f"{domain.data['id']} does not support {mode}")

    outcomes = Counter({
        "completed": 0,
        "dead": 0,
        "retreated": 0,
        "action_limit": 0,
        "objective_blocked": 0,
        "blocked": 0,
        "timeout": 0,
    })
    ghost_id = _starter_id(registry, domain)
    with tempfile.TemporaryDirectory(prefix="ghost-chapter-sim-") as temp:
        repository = GameRepository(Path(temp) / "campaign.sqlite3")
        solo = SoloRunService(repository, registry)
        party = PartyRunService(repository, registry)
        handler = CommandHandler(repository, registry, solo, party)
        for sample in range(samples):
            group_id = f"sim-{domain.data['id']}-{mode}-{difficulty}-{sample}"
            roles = ("leader", "member") if mode == "party" else ("leader",)
            identities = {
                role: Identity(
                    "aiocqhttp", "aiocqhttp", f"{seed[-8:]}-{sample}-{role}",
                    group_id if mode == "party" else "", f"recruit-{sample}-{role}",
                )
                for role in roles
            }
            profile_ids = {
                role: _prepare_profile(handler, repository, identities[role], domain, ghost_id)
                for role in roles
            }
            difficulty_name = {"normal": "普通", "hard": "困难"}.get(difficulty, difficulty)
            mode_name = "团队" if mode == "party" else "单人"
            request = f"{domain.data.get('name', domain.data['id'])} {mode_name} {difficulty_name}"
            handler.open_run(identities["leader"], request)
            run = repository.get_active_run(identities["leader"])
            if not run:
                outcomes["blocked"] += 1
                continue
            if mode == "party":
                handler.join(identities["member"], str(run["invite_code"]))
                handler.ready(identities["leader"])
                handler.ready(identities["member"])
                handler.start_party(identities["leader"])
                run = repository.get_active_run(identities["leader"])
                if not run or run["status"] != "active":
                    outcomes["blocked"] += 1
                    continue
            state = run["state"]
            trial_seed = f"{seed}:{domain.data['id']}:{domain.version}:{mode}:{difficulty}:{sample}"
            state["rng"] = new_rng_state(trial_seed)
            with repository.transaction() as connection:
                repository.save_run_state_in(connection, str(run["run_id"]), int(run["version"]), state)
            trial = _ChapterTrial.__new__(_ChapterTrial)
            trial.registry = registry
            trial.domain = domain
            trial.mode = mode
            trial.difficulty = difficulty
            trial.repository = repository
            trial.solo = solo
            trial.party = party
            trial.handler = handler
            trial.sequence = 0
            trial.identities = identities
            trial.profile_ids = profile_ids
            trial.investigated_nodes = set()
            trial.simulated_day_rollovers = 0
            trial.domain_data = domain.data
            trial.ghost_id = ghost_id
            trial.seed = trial_seed
            trial.run_id = str(run["run_id"])
            outcomes[trial.run()] += 1
    return dict(outcomes)


def _latest_domains(registry: ContentRegistry) -> list[Any]:
    latest: dict[str, Any] = {}
    for entry in registry.entries.values():
        if entry.content_type != "domains":
            continue
        domain_id = str(entry.data.get("id", ""))
        current = latest.get(domain_id)
        if current is None or ContentRegistry._version_key(entry.version) > ContentRegistry._version_key(current.version):
            latest[domain_id] = entry
    return [latest[key] for key in sorted(latest)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default="seven_school", help="domain ID, or 'all' (default: seven_school)")
    parser.add_argument("--mode", choices=("solo", "party", "both"), default="both")
    parser.add_argument("--samples", type=int, default=50, help="trials per domain/mode/difficulty")
    parser.add_argument("--seed", default="ghost-domain-chapter-v1", help="base deterministic seed")
    args = parser.parse_args(argv)
    if args.samples < 1:
        parser.error("--samples must be at least 1")
    if not args.seed.strip():
        parser.error("--seed must not be empty")

    registry = ContentRegistry(Path(__file__).resolve().parents[1] / "content" / "packs")
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

    print("# Full-chapter outcomes use the scripted reference policy; they are not a human win-rate prediction.")
    print(f"# samples_per_row={args.samples}; base_seed={args.seed}; max_actions={_MAX_STEPS}; max_day_rollovers={_MAX_DAY_ROLLOVERS}")
    print("# Policy: one manifest starter ghost per player; investigate each route node with clues once; choose the first currently available dialogue option; observe required goal rules and use recognizable configured counteractions; focus lowest-HP enemy with strongest ready attack skill. Full HP/SAN and normal mission kit per trial; official action budget applies.")
    writer = csv.writer(sys.stdout)
    writer.writerow(["domain_id", "pack_version", "mode", "difficulty", "samples", *_OUTCOMES, "completion_rate", "completion_95ci_low", "completion_95ci_high"])
    for domain in domains:
        modes = [mode for mode in ("solo", "party") if mode in domain.data.get("supported_modes", [])]
        if args.mode != "both":
            modes = [mode for mode in modes if mode == args.mode]
        difficulties = sorted(domain.data.get("difficulty_levels", {"normal": {}}))
        for mode in modes:
            for difficulty in difficulties:
                outcomes = simulate_chapter(registry, domain, mode, difficulty, args.samples, args.seed)
                low, high = wilson_interval(outcomes["completed"], args.samples)
                writer.writerow([
                    domain.data["id"], domain.version, mode, difficulty, args.samples,
                    *(outcomes[name] for name in _OUTCOMES),
                    f"{outcomes['completed'] / args.samples:.4f}", f"{low:.4f}", f"{high:.4f}",
                ])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
