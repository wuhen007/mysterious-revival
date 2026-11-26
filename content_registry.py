from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .engine.models import ContentEntry, ValidationIssue
from .engine.content_contracts import (
    SUPPORTED_CAMPAIGN_REWARD_TYPES,
    SUPPORTED_GHOST_BEHAVIOR_KINDS,
    SUPPORTED_GHOST_SKILL_TYPES,
    SUPPORTED_ITEM_EFFECT_TYPES,
    SUPPORTED_BEHAVIOR_DISTANCE_VALUES,
    SUPPORTED_BEHAVIOR_TARGET_STRATEGIES,
    SUPPORTED_STORY_CONDITION_TYPES,
    SUPPORTED_STORY_EFFECT_TYPES,
)

_CONTENT_TYPES: dict[str, dict[str, type | tuple[type, ...]]] = {
    "domains": {
        "chapter": int,
        "supported_modes": list,
        "party_size": dict,
        "mission_kit": list,
        "nodes": list,
        "goals": list,
        "encounters": list,
    },
    "ghosts": {
        "stats": dict,
        "capture_threshold": (int, float),
        "capture_difficulty": (int, float),
        "base_loyalty": (int, float),
        "starter_weight": (int, float),
        "skills": list,
        "rules": list,
        "behavior": dict,
    },
    "items": {"target_types": list, "uses": int, "effects": list},
    "characters": {
        "domain_ref": str,
        "location_ref": str,
        "dialogue_nodes": list,
    },
    "stories": {"domain_ref": str, "trigger": dict, "nodes": list},
}

_COMMON_FIELDS = ("id", "schema_version", "name", "description")
_REFERENCE_FIELDS = {
    "domain_ref": "domains",
    "ghost_ref": "ghosts",
    "item_ref": "items",
    "character_ref": "characters",
    "story_ref": "stories",
}
# 不指向内容包对象、仅作为字符串标识保存的引用（如传承能力 id）。
_OPAQUE_REF_FIELDS = {"ability_ref"}

class ContentRegistry:
    """Loads immutable versioned JSON packs and exposes only fully valid packs."""

    def __init__(self, packs_root: Path):
        self.packs_root = Path(packs_root)
        self.entries: dict[tuple[str, str, str, str], ContentEntry] = {}
        self.manifests: dict[tuple[str, str], dict[str, Any]] = {}
        self._entry_paths: dict[tuple[str, str, str, str], Path] = {}
        self.issues: list[ValidationIssue] = []

    def load(self) -> list[ValidationIssue]:
        self.entries.clear()
        self.manifests.clear()
        self._entry_paths.clear()
        self.issues.clear()
        if not self.packs_root.is_dir():
            self._issue(self.packs_root, "$", "content pack directory does not exist")
            return self.issues

        seen_pack_versions: set[tuple[str, str]] = set()
        for pack_dir in sorted(path for path in self.packs_root.iterdir() if path.is_dir()):
            for version_dir in sorted(path for path in pack_dir.iterdir() if path.is_dir()):
                self._load_pack(pack_dir, version_dir, seen_pack_versions)
        domains = [
            (self._entry_paths[key], entry.data)
            for key, entry in self.entries.items()
            if entry.content_type == "domains"
        ]
        self._validate_campaign_links(domains)
        return list(self.issues)

    def find_domain(self, name_or_id: str) -> ContentEntry | None:
        needle = name_or_id.strip().casefold()
        matches: list[ContentEntry] = []
        for (content_type, _pack_id, _version, _object_id), entry in self.entries.items():
            if content_type != "domains":
                continue
            aliases = entry.data.get("aliases", [])
            candidates = [entry.data.get("id", ""), entry.data.get("name", ""), *aliases]
            if any(str(candidate).casefold() == needle for candidate in candidates):
                matches.append(entry)
        return max(matches, key=lambda entry: self._version_key(entry.version), default=None)

    @staticmethod
    def _version_key(version: str) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in version.lstrip("v").split("."))
        except ValueError:
            return (0,)

    def get(
        self, content_type: str, object_id: str, pack_id: str, version: str
    ) -> ContentEntry | None:
        return self.entries.get((content_type, pack_id, version, object_id))

    def get_manifest(self, pack_id: str, version: str) -> dict[str, Any] | None:
        return self.manifests.get((pack_id, version))

    def list_entries(self, content_type: str, pack_id: str, version: str) -> list[ContentEntry]:
        return [
            entry
            for (kind, entry_pack, entry_version, _object_id), entry in self.entries.items()
            if kind == content_type and entry_pack == pack_id and entry_version == version
        ]

    def find_entry(self, content_type: str, name_or_id: str, pack_id: str, version: str) -> ContentEntry | None:
        needle = name_or_id.strip().casefold()
        for (kind, entry_pack, entry_version, _object_id), entry in self.entries.items():
            if (
                kind == content_type
                and entry_pack == pack_id
                and entry_version == version
                and (entry.data.get("id", "").casefold() == needle or entry.data.get("name", "").casefold() == needle)
            ):
                return entry
        return None

    def _load_pack(
        self,
        pack_dir: Path,
        version_dir: Path,
        seen_pack_versions: set[tuple[str, str]],
    ) -> None:
        first_issue = len(self.issues)
        manifest_path = version_dir / "manifest.json"
        manifest = self._read_json(manifest_path)
        if manifest is None:
            return
        if not isinstance(manifest, dict):
            self._issue(manifest_path, "$", "manifest must be a JSON object")
            return
        for field in ("pack_id", "version", "requires_engine_version"):
            if not isinstance(manifest.get(field), str) or not manifest[field].strip():
                self._issue(manifest_path, f"$.{field}", "required non-empty string")
        if manifest.get("pack_id") != pack_dir.name:
            self._issue(manifest_path, "$.pack_id", "must match the pack directory name")
        if manifest.get("version") != version_dir.name:
            self._issue(manifest_path, "$.version", "must match the version directory name")
        pack_id = manifest.get("pack_id")
        version = manifest.get("version")
        if not isinstance(pack_id, str) or not isinstance(version, str):
            return
        pack_key = (pack_id, version)
        if pack_key in seen_pack_versions:
            self._issue(manifest_path, "$", f"duplicate pack version {pack_id}@{version}")
        seen_pack_versions.add(pack_key)

        starter_pool = manifest.get("starter_ghost_pool", [])
        if not isinstance(starter_pool, list):
            self._issue(manifest_path, "$.starter_ghost_pool", "expected array")
            starter_pool = []
        for index, starter in enumerate(starter_pool):
            starter_path = f"$.starter_ghost_pool[{index}]"
            if not isinstance(starter, dict):
                self._issue(manifest_path, starter_path, "expected object")
                continue
            if not isinstance(starter.get("ghost_ref"), str) or not starter["ghost_ref"]:
                self._issue(manifest_path, f"{starter_path}.ghost_ref", "required non-empty string")
            weight = starter.get("weight", 1)
            if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
                self._issue(manifest_path, f"{starter_path}.weight", "expected positive number")

        objects: dict[str, list[tuple[Path, dict[str, Any]]]] = {
            content_type: [] for content_type in _CONTENT_TYPES
        }
        ids: dict[str, set[str]] = {content_type: set() for content_type in _CONTENT_TYPES}
        for content_type in _CONTENT_TYPES:
            type_dir = version_dir / content_type
            if not type_dir.exists():
                continue
            if not type_dir.is_dir():
                self._issue(type_dir, "$", "content type path must be a directory")
                continue
            for file_path in sorted(type_dir.glob("*.json")):
                data = self._read_json(file_path)
                if not isinstance(data, dict):
                    if data is not None:
                        self._issue(file_path, "$", "content object must be a JSON object")
                    continue
                self._validate_object(content_type, data, file_path)
                object_id = data.get("id")
                if isinstance(object_id, str) and object_id:
                    if object_id in ids[content_type]:
                        self._issue(file_path, "$.id", f"duplicate {content_type} id {object_id!r}")
                    ids[content_type].add(object_id)
                objects[content_type].append((file_path, data))

        for index, starter in enumerate(starter_pool):
            if isinstance(starter, dict) and isinstance(starter.get("ghost_ref"), str) and starter["ghost_ref"] not in ids["ghosts"]:
                    self._issue(
                        manifest_path,
                        f"$.starter_ghost_pool[{index}].ghost_ref",
                        f"reference {starter['ghost_ref']!r} does not exist in this pack version",
                    )

        nested_ids = self._collect_nested_ids(objects)
        self._validate_references(objects, ids, nested_ids)
        if len(self.issues) != first_issue:
            return

        self.manifests[pack_key] = manifest
        for content_type, entries in objects.items():
            for _file_path, data in entries:
                entry = ContentEntry(content_type, pack_id, version, data)
                entry_key = (content_type, pack_id, version, data["id"])
                self.entries[entry_key] = entry
                self._entry_paths[entry_key] = _file_path

    def _validate_object(self, content_type: str, data: dict[str, Any], path: Path) -> None:
        required = {field: str for field in _COMMON_FIELDS}
        required["schema_version"] = int
        required.update(_CONTENT_TYPES[content_type])
        for field, expected in required.items():
            value = data.get(field)
            if field not in data:
                self._issue(path, f"$.{field}", "required field is missing")
            elif not self._matches(value, expected):
                expected_name = (
                    "number" if expected == (int, float) else self._type_name(expected)
                )
                self._issue(path, f"$.{field}", f"expected {expected_name}")
        schema_version = data.get("schema_version")
        if isinstance(schema_version, int) and not isinstance(schema_version, bool) and schema_version < 1:
            self._issue(path, "$.schema_version", "must be a positive integer")
        if content_type == "ghosts":
            skills = data.get("skills", [])
            if isinstance(skills, list):
                for index, skill in enumerate(skills):
                    if not isinstance(skill, dict):
                        continue
                    skill_type = skill.get("type", "attack")
                    if not isinstance(skill_type, str) or skill_type not in SUPPORTED_GHOST_SKILL_TYPES:
                        self._issue(
                            path,
                            f"$.skills[{index}].type",
                            f"unsupported ghost skill type {skill_type!r}",
                        )
            self._validate_behavior_fields(
                data.get("behavior", {}), "$.behavior", path, require_kind=True
            )
            stats = data.get("stats", {})
            if isinstance(stats, dict):
                stat_minimums = {"hp": 1, "attack": 1, "defense": 0, "speed": 0}
                for stat, minimum in stat_minimums.items():
                    value = stats.get(stat)
                    stat_path = f"$.stats.{stat}"
                    if stat not in stats:
                        self._issue(path, stat_path, "required field is missing")
                    elif isinstance(value, bool) or not isinstance(value, int):
                        self._issue(path, stat_path, "expected integer")
                    elif value < minimum:
                        self._issue(path, stat_path, f"must be at least {minimum}")

            rules = data.get("rules", [])
            if isinstance(rules, list):
                for index, rule in enumerate(rules):
                    if not isinstance(rule, dict):
                        continue
                    rule_path = f"$.rules[{index}]"
                    clues = rule.get("clues")
                    if not isinstance(clues, list) or not clues or any(
                        not isinstance(clue, str) or not clue.strip() for clue in clues
                    ):
                        self._issue(path, f"{rule_path}.clues", "expected non-empty array of strings")
                    elif "reveal_after" in rule:
                        reveal_after = rule["reveal_after"]
                        if isinstance(reveal_after, bool) or not isinstance(reveal_after, int):
                            self._issue(path, f"{rule_path}.reveal_after", "expected integer")
                        elif reveal_after < 1 or reveal_after > len(clues):
                            self._issue(path, f"{rule_path}.reveal_after", "must be within the clue count")
                    if not isinstance(rule.get("counter_method"), str) or not rule["counter_method"].strip():
                        self._issue(path, f"{rule_path}.counter_method", "required non-empty string")
                    counteractions = rule.get("counteractions", [])
                    if not isinstance(counteractions, list):
                        self._issue(path, f"{rule_path}.counteractions", "expected array")
                    else:
                        for counter_index, counteraction in enumerate(counteractions):
                            if not isinstance(counteraction, str) or not counteraction.strip():
                                self._issue(
                                    path,
                                    f"{rule_path}.counteractions[{counter_index}]",
                                    "expected non-empty string",
                                )
                    if "discovery_san_cost" in rule:
                        cost = rule["discovery_san_cost"]
                        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
                            self._issue(path, f"{rule_path}.discovery_san_cost", "expected non-negative integer")
                    if "story_flag_on_counteraction" in rule and (
                        not isinstance(rule["story_flag_on_counteraction"], str)
                        or not rule["story_flag_on_counteraction"].strip()
                    ):
                        self._issue(path, f"{rule_path}.story_flag_on_counteraction", "expected non-empty string")

            if isinstance(skills, list):
                for index, skill in enumerate(skills):
                    if not isinstance(skill, dict):
                        continue
                    skill_path = f"$.skills[{index}]"
                    if not isinstance(skill.get("name"), str) or not skill["name"].strip():
                        self._issue(path, f"{skill_path}.name", "required non-empty string")
                    power = skill.get("power")
                    if power is None:
                        self._issue(path, f"{skill_path}.power", "required number")
                    elif isinstance(power, bool) or not isinstance(power, (int, float)):
                        self._issue(path, f"{skill_path}.power", "expected number")
                    elif power <= 0:
                        self._issue(path, f"{skill_path}.power", "must be positive")
                    for numeric in ("cooldown_turns", "recovery_cost"):
                        if numeric not in skill:
                            continue
                        value = skill[numeric]
                        if isinstance(value, bool) or not isinstance(value, int):
                            self._issue(path, f"{skill_path}.{numeric}", "expected integer")
                        elif value < 0:
                            self._issue(path, f"{skill_path}.{numeric}", "must be non-negative")
        if content_type == "characters":
            for index, dialogue in enumerate(data.get("dialogue_nodes", [])):
                if not isinstance(dialogue, dict):
                    continue
                dialogue_path = f"$.dialogue_nodes[{index}]"
                if not isinstance(dialogue.get("text"), str) or not dialogue.get("text"):
                    self._issue(path, f"{dialogue_path}.text", "required non-empty string")
                choices = dialogue.get("choices", [])
                if not isinstance(choices, list):
                    self._issue(path, f"{dialogue_path}.choices", "expected array")
                    continue
                seen_choices: set[str] = set()
                for choice_index, choice in enumerate(choices):
                    choice_path = f"{dialogue_path}.choices[{choice_index}]"
                    if not isinstance(choice, dict):
                        self._issue(path, choice_path, "expected object")
                        continue
                    choice_id = choice.get("id")
                    if not isinstance(choice_id, str) or not choice_id:
                        self._issue(path, f"{choice_path}.id", "required non-empty string")
                    elif choice_id in seen_choices:
                        self._issue(path, f"{choice_path}.id", f"duplicate choice id {choice_id!r}")
                    else:
                        seen_choices.add(choice_id)
                    if not isinstance(choice.get("label"), str) or not choice.get("label"):
                        self._issue(path, f"{choice_path}.label", "required non-empty string")
        if content_type == "stories":
            trigger = data.get("trigger", {})
            if not isinstance(trigger, dict) or not isinstance(trigger.get("conditions", []), list):
                self._issue(path, "$.trigger.conditions", "expected array")
            for index, node in enumerate(data.get("nodes", [])):
                if not isinstance(node, dict):
                    continue
                node_path = f"$.nodes[{index}]"
                if "location_ref" in node and (not isinstance(node.get("location_ref"), str) or not node.get("location_ref")):
                    self._issue(path, f"{node_path}.location_ref", "expected non-empty location reference")
                if not isinstance(node.get("text"), str) or not node.get("text"):
                    self._issue(path, f"{node_path}.text", "required non-empty string")
        if content_type == "domains":
            modes = data.get("supported_modes")
            if isinstance(modes, list):
                for index, mode in enumerate(modes):
                    if mode not in {"solo", "party"}:
                        self._issue(path, f"$.supported_modes[{index}]", "expected 'solo' or 'party'")
            levels = data.get("difficulty_levels", {})
            if not isinstance(levels, dict):
                self._issue(path, "$.difficulty_levels", "expected object")
            else:
                for difficulty, config in levels.items():
                    if not isinstance(config, dict):
                        self._issue(path, f"$.difficulty_levels.{difficulty}", "expected object")
                        continue
                    if "unlocked_by_default" in config and not isinstance(config["unlocked_by_default"], bool):
                        self._issue(path, f"$.difficulty_levels.{difficulty}.unlocked_by_default", "expected boolean")
                    if "minimum_party_actions" in config and (not isinstance(config["minimum_party_actions"], int) or config["minimum_party_actions"] < 0):
                        self._issue(path, f"$.difficulty_levels.{difficulty}.minimum_party_actions", "expected non-negative integer")
                    if "rewards" in config and not isinstance(config["rewards"], list):
                        self._issue(path, f"$.difficulty_levels.{difficulty}.rewards", "expected array")
            unlock = data.get("unlock", {})
            if not isinstance(unlock, dict):
                self._issue(path, "$.unlock", "expected object")
            else:
                if "optional" in unlock and not isinstance(unlock["optional"], bool):
                    self._issue(path, "$.unlock.optional", "expected boolean")
                clues_required = unlock.get("requires_clues")
                if clues_required is not None and (
                    isinstance(clues_required, bool)
                    or not isinstance(clues_required, int)
                    or clues_required < 0
                ):
                    self._issue(path, "$.unlock.requires_clues", "expected non-negative integer")
                for field in ("requires_completed", "unlocks"):
                    values = unlock.get(field, [])
                    if not isinstance(values, list):
                        self._issue(path, f"$.unlock.{field}", "expected array")
                        continue
                    seen_refs: set[str] = set()
                    for index, value in enumerate(values):
                        ref_path = f"$.unlock.{field}[{index}]"
                        if not isinstance(value, str) or not value.strip():
                            self._issue(path, ref_path, "expected non-empty domain id")
                        elif value in seen_refs:
                            self._issue(path, ref_path, f"duplicate domain id {value!r}")
                        else:
                            seen_refs.add(value)

            nodes = data.get("nodes", [])
            if isinstance(nodes, list) and not nodes:
                self._issue(path, "$.nodes", "must contain at least one route node")

            extraction = data.get("extraction")
            if extraction is not None:
                if not isinstance(extraction, dict):
                    self._issue(path, "$.extraction", "expected object")
                elif not isinstance(extraction.get("location_ref"), str) or not extraction["location_ref"].strip():
                    self._issue(path, "$.extraction.location_ref", "required non-empty node reference")

            goals = data.get("goals", [])
            if isinstance(goals, list):
                if not goals:
                    self._issue(path, "$.goals", "must contain at least one objective")
                for index, goal in enumerate(goals):
                    goal_path = f"$.goals[{index}]"
                    if not isinstance(goal, dict):
                        self._issue(path, goal_path, "expected object")
                        continue
                    if not isinstance(goal.get("id"), str) or not goal["id"].strip():
                        self._issue(path, f"{goal_path}.id", "required non-empty string")
                    if not isinstance(goal.get("description"), str) or not goal["description"].strip():
                        self._issue(path, f"{goal_path}.description", "required non-empty string")
                    if "required" in goal and not isinstance(goal["required"], bool):
                        self._issue(path, f"{goal_path}.required", "expected boolean")
                    if "conditions" in goal and not isinstance(goal["conditions"], list):
                        self._issue(path, f"{goal_path}.conditions", "expected array")

            variants = data.get("difficulty_variants", {})
            if not isinstance(variants, dict):
                self._issue(path, "$.difficulty_variants", "expected object")
        for nested_field in ("nodes", "encounters", "skills", "rules", "dialogue_nodes"):
            nested = data.get(nested_field)
            if not isinstance(nested, list):
                continue
            nested_seen: set[str] = set()
            for index, item in enumerate(nested):
                item_path = f"$.{nested_field}[{index}]"
                if not isinstance(item, dict):
                    self._issue(path, item_path, "expected object")
                    continue
                nested_id = item.get("id")
                if not isinstance(nested_id, str) or not nested_id:
                    self._issue(path, f"{item_path}.id", "required non-empty string")
                elif nested_id in nested_seen:
                    self._issue(path, f"{item_path}.id", f"duplicate id {nested_id!r}")
                else:
                    nested_seen.add(nested_id)

    def _validate_campaign_links(
        self, domains: list[tuple[Path, dict[str, Any]]]
    ) -> None:
        domain_ids = {
            str(data["id"])
            for _path, data in domains
            if isinstance(data.get("id"), str) and data["id"]
        }
        latest: dict[str, tuple[tuple[int, ...], Path, dict[str, Any]]] = {}
        for path, data in domains:
            domain_id = data.get("id")
            if not isinstance(domain_id, str) or not domain_id:
                continue
            for field in ("requires_completed", "unlocks"):
                refs = data.get("unlock", {}).get(field, [])
                if not isinstance(refs, list):
                    continue
                for index, reference in enumerate(refs):
                    if not isinstance(reference, str) or not reference:
                        continue
                    ref_path = f"$.unlock.{field}[{index}]"
                    if reference not in domain_ids:
                        self._issue(
                            path,
                            ref_path,
                            f"domain reference {reference!r} does not exist in any loaded content pack",
                        )
                    elif reference == domain_id:
                        self._issue(path, ref_path, "a domain cannot reference itself in campaign progression")

            version = self._version_key(path.parent.name)
            previous = latest.get(domain_id)
            if previous is None or version > previous[0]:
                latest[domain_id] = (version, path, data)

        graph_specs = (
            ("requires_completed", "cyclic campaign prerequisites"),
            ("unlocks", "cyclic campaign unlocks"),
        )
        for field, label in graph_specs:
            graph: dict[str, set[str]] = {}
            graph_paths: dict[str, Path] = {}
            for domain_id, (_version, path, data) in latest.items():
                refs = data.get("unlock", {}).get(field, [])
                graph_paths[domain_id] = path
                graph[domain_id] = {
                    reference
                    for reference in refs
                    if isinstance(reference, str) and reference in latest
                } if isinstance(refs, list) else set()

            visited: set[str] = set()
            active: list[str] = []
            reported_cycles: set[frozenset[str]] = set()

            def visit(domain_id: str) -> None:
                if domain_id in visited:
                    return
                if domain_id in active:
                    start = active.index(domain_id)
                    cycle = frozenset(active[start:])
                    if cycle not in reported_cycles:
                        reported_cycles.add(cycle)
                        self._issue(
                            graph_paths[domain_id],
                            f"$.unlock.{field}",
                            f"{label}: {' -> '.join(active[start:] + [domain_id])}",
                        )
                    return
                active.append(domain_id)
                for linked_id in sorted(graph.get(domain_id, set())):
                    visit(linked_id)
                active.pop()
                visited.add(domain_id)

            for domain_id in sorted(graph):
                visit(domain_id)

    def _validate_behavior_fields(
        self, behavior: Any, json_path: str, file_path: Path, require_kind: bool
    ) -> None:
        if not isinstance(behavior, dict):
            self._issue(file_path, json_path, "expected object")
            return

        if require_kind or "kind" in behavior:
            kind = behavior.get("kind", "brute")
            if not isinstance(kind, str) or kind not in SUPPORTED_GHOST_BEHAVIOR_KINDS:
                self._issue(file_path, f"{json_path}.kind", f"unsupported behavior kind {kind!r}")

        if "target_strategy" in behavior:
            strategy = behavior["target_strategy"]
            if not isinstance(strategy, str) or strategy not in SUPPORTED_BEHAVIOR_TARGET_STRATEGIES:
                self._issue(file_path, f"{json_path}.target_strategy", f"unsupported target strategy {strategy!r}")
        if "required_distance" in behavior:
            distance = behavior["required_distance"]
            if not isinstance(distance, str) or distance not in SUPPORTED_BEHAVIOR_DISTANCE_VALUES:
                self._issue(file_path, f"{json_path}.required_distance", f"unsupported distance {distance!r}")

        for field in ("counter_name", "damage_resets_counter", "light_status"):
            if field in behavior and (not isinstance(behavior[field], str) or not behavior[field].strip()):
                self._issue(file_path, f"{json_path}.{field}", "expected non-empty string")
        if "supernatural" in behavior and not isinstance(behavior["supernatural"], bool):
            self._issue(file_path, f"{json_path}.supernatural", "expected boolean")

        positive_numbers = (
            "attack_multiplier",
            "strong_attack_multiplier",
            "attack_multiplier_in_dark",
            "light_damage_multiplier",
        )
        for field in positive_numbers:
            if field not in behavior:
                continue
            value = behavior[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                self._issue(file_path, f"{json_path}.{field}", "expected number")
            elif value <= 0:
                self._issue(file_path, f"{json_path}.{field}", "must be positive")

        integer_minimums = {
            "knocks_before_attack": 1,
            "infection_damage": 0,
            "infection_san_damage": 0,
            "infection_rounds": 0,
            "light_skip_actions": 0,
        }
        for field, minimum in integer_minimums.items():
            if field not in behavior:
                continue
            value = behavior[field]
            if isinstance(value, bool) or not isinstance(value, int):
                self._issue(file_path, f"{json_path}.{field}", "expected integer")
            elif value < minimum:
                self._issue(file_path, f"{json_path}.{field}", f"must be at least {minimum}")

    def _collect_nested_ids(
        self, objects: dict[str, list[tuple[Path, dict[str, Any]]]]
    ) -> dict[str, dict[str, set[str]]]:
        indexes: dict[str, dict[str, set[str]]] = {
            name: {} for name in ("nodes", "skills", "rules", "dialogue_nodes")
        }
        for entries in objects.values():
            for _path, data in entries:
                owner_id = str(data.get("id", ""))
                for field, owner_index in indexes.items():
                    values = data.get(field)
                    if isinstance(values, list):
                        owner_index.setdefault(owner_id, set()).update(
                            item["id"] for item in values if isinstance(item, dict) and isinstance(item.get("id"), str)
                        )
        return indexes

    def _validate_references(
        self,
        objects: dict[str, list[tuple[Path, dict[str, Any]]]],
        ids: dict[str, set[str]],
        nested_ids: dict[str, dict[str, set[str]]],
    ) -> None:
        for content_type, entries in objects.items():
            for path, data in entries:
                owner_domain = data.get("id") if content_type == "domains" else data.get("domain_ref")
                self._walk_references(data, "$", path, owner_domain, ids, nested_ids, content_type)

    def _walk_references(
        self,
        value: Any,
        json_path: str,
        file_path: Path,
        owner_domain: Any,
        ids: dict[str, set[str]],
        nested_ids: dict[str, dict[str, set[str]]],
        content_type: str,
    ) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{json_path}.{key}"
                if key in _REFERENCE_FIELDS:
                    self._check_reference(child, child_path, file_path, ids[_REFERENCE_FIELDS[key]])
                elif key in {"node_ref", "next_node_ref", "location_ref"}:
                    known = nested_ids["nodes"].get(str(owner_domain), set())
                    self._check_reference(child, child_path, file_path, known)
                elif key == "skill_ref":
                    known = set().union(*nested_ids["skills"].values()) if nested_ids["skills"] else set()
                    self._check_reference(child, child_path, file_path, known)
                elif key in {"rule_ref", "boss_rule"}:
                    known = set().union(*nested_ids["rules"].values()) if nested_ids["rules"] else set()
                    self._check_reference(child, child_path, file_path, known)
                elif key in _OPAQUE_REF_FIELDS:
                    if not isinstance(child, str) or not child:
                        self._issue(file_path, child_path, "reference must be a non-empty string")
                elif key.endswith("_ref"):
                    self._issue(file_path, child_path, f"unknown reference field {key!r}")
                if key == "enemy_behavior_overrides":
                    if content_type != "domains":
                        self._issue(file_path, child_path, "behavior overrides are only supported on domains")
                    elif not isinstance(child, dict):
                        self._issue(file_path, child_path, "expected object")
                    else:
                        for ghost_id, override in child.items():
                            override_path = f"{child_path}.{ghost_id}"
                            self._check_reference(ghost_id, override_path, file_path, ids["ghosts"])
                            self._validate_behavior_fields(
                                override, override_path, file_path, require_kind=False
                            )
                elif key == "behavior_overrides":
                    if content_type != "domains":
                        self._issue(file_path, child_path, "behavior overrides are only supported on domains")
                    else:
                        self._validate_behavior_fields(
                            child, child_path, file_path, require_kind=False
                        )
                if key in {"effects", "conditions", "rewards"}:
                    self._validate_effects_or_conditions(
                        child, key, child_path, file_path, content_type
                    )
                self._walk_references(
                    child, child_path, file_path, owner_domain, ids, nested_ids, content_type
                )
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self._walk_references(child, f"{json_path}[{index}]", file_path, owner_domain, ids, nested_ids, content_type)

    def _validate_effects_or_conditions(
        self, value: Any, field: str, json_path: str, file_path: Path, content_type: str
    ) -> None:
        if field == "conditions":
            allowed = SUPPORTED_STORY_CONDITION_TYPES
            label = "condition"
        elif field == "rewards":
            allowed = SUPPORTED_CAMPAIGN_REWARD_TYPES if content_type == "domains" else frozenset()
            label = "campaign reward"
        elif content_type == "items":
            allowed = SUPPORTED_ITEM_EFFECT_TYPES
            label = "item effect"
        elif content_type in {"characters", "stories"}:
            allowed = SUPPORTED_STORY_EFFECT_TYPES
            label = "story effect"
        else:
            allowed = frozenset()
            label = "effect"
        required_fields = {
            "owns_item": ("item_ref",),
            "hp_below": ("ratio",),
            "rule_discovered": ("rule_ref",),
            "rule_countered": ("rule_ref",),
            "story_flag_matches": ("flag", "value"),
            "difficulty_at_least": ("difficulty",),
            "counter_at_least": ("counter", "value"),
            "location_is": ("location_ref",),
            "damage": ("amount",),
            "heal": ("amount",),
            "san_change": ("amount",),
            "item_add": ("item_ref",),
            "item_remove": ("item_ref",),
            "reveal_rule": ("rule_ref",),
            "counter_rule": ("rule_ref",),
            "loyalty_change": ("amount",),
            "set_story_flag": ("flag",),
            "increment_counter": ("counter",),
            "move_location": ("location_ref",),
            "capture_bonus": ("amount",),
            "resource_reward": ("resource", "amount"),
            "unlock_domain": ("domain_ref",),
            "unlock_difficulty": ("difficulty",),
            "add_ghost_slot": ("amount",),
            "unlock_ability": ("ability_ref",),
        }
        if not isinstance(value, list):
            self._issue(file_path, json_path, "expected array")
            return
        items = value
        for index, item in enumerate(items):
            item_path = f"{json_path}[{index}]" if isinstance(value, list) else json_path
            if not isinstance(item, dict):
                self._issue(file_path, item_path, "expected object")
                continue
            effect_type = item.get("type")
            if not isinstance(effect_type, str) or effect_type not in allowed:
                self._issue(file_path, f"{item_path}.type", f"unsupported {label} type {effect_type!r}")
                continue
            for required in required_fields.get(str(effect_type), ()):
                if required not in item:
                    self._issue(file_path, f"{item_path}.{required}", "required field is missing")
            for numeric in ("amount", "quantity", "ratio"):
                if numeric in item and (isinstance(item[numeric], bool) or not isinstance(item[numeric], (int, float))):
                    self._issue(file_path, f"{item_path}.{numeric}", "expected number")
            if effect_type == "counter_at_least" and (isinstance(item.get("value"), bool) or not isinstance(item.get("value"), (int, float))):
                self._issue(file_path, f"{item_path}.value", "expected number")

    def _check_reference(
        self, value: Any, json_path: str, file_path: Path, known_ids: set[str]
    ) -> None:
        if not isinstance(value, str) or not value:
            self._issue(file_path, json_path, "reference must be a non-empty string")
        elif value not in known_ids:
            self._issue(file_path, json_path, f"reference {value!r} does not exist in this pack version")

    def _read_json(self, path: Path) -> Any | None:
        try:
            with path.open("r", encoding="utf-8-sig") as stream:
                return json.load(stream)
        except FileNotFoundError:
            self._issue(path, "$", "file does not exist")
        except json.JSONDecodeError as error:
            self._issue(path, f"line {error.lineno}, column {error.colno}", error.msg)
        except OSError as error:
            self._issue(path, "$", str(error))
        return None

    def _issue(self, path: Path, json_path: str, message: str) -> None:
        try:
            shown_path = path.relative_to(self.packs_root.parent).as_posix()
        except ValueError:
            shown_path = path.as_posix()
        self.issues.append(ValidationIssue(shown_path, json_path, message))

    @staticmethod
    def _matches(value: Any, expected: type | tuple[type, ...]) -> bool:
        if expected == (int, float):
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected is int:
            return isinstance(value, int) and not isinstance(value, bool)
        return isinstance(value, expected)

    @staticmethod
    def _type_name(expected: type | tuple[type, ...]) -> str:
        if isinstance(expected, tuple):
            return " or ".join(item.__name__ for item in expected)
        return expected.__name__
