"""Legacy generator for the historical ``canon_*.py`` authoring data.

The current runtime source of truth is the immutable, versioned JSON under
``content/packs``. This generator does not reproduce the restored JSON packs;
do not run it against the current pack tree. Existing same-version differences
are rejected rather than overwritten. Keep this module only for isolated legacy
exports until its source is reconciled or retired.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from .canon_ghosts import GHOSTS as _BASE_GHOSTS
from .canon_ghosts import ITEMS as _BASE_ITEMS
from .canon_npcs import CHARACTERS as _BASE_CHARACTERS
from .canon_npcs import STORIES
from .canon_seven import SEVEN_CHARACTERS, SEVEN_DOMAINS, SEVEN_GHOSTS, SEVEN_ITEMS, SEVEN_STORIES
from .canon_world import DOMAINS as _BASE_DOMAINS

GHOSTS = [*_BASE_GHOSTS, *SEVEN_GHOSTS]
ITEMS = [*_BASE_ITEMS, *SEVEN_ITEMS]
DOMAINS = [*_BASE_DOMAINS, *SEVEN_DOMAINS]
CHARACTERS = [*_BASE_CHARACTERS, *SEVEN_CHARACTERS]
STORIES = [*STORIES, *SEVEN_STORIES]

PACK_VERSION = "1.0.0"
ENGINE_VERSION = ">=0.3.0"
DEEP_PACK = "faction_archive"

_GHOSTS = {g["id"]: g for g in GHOSTS}
_ITEMS = {i["id"]: i for i in ITEMS}
_RULE_OWNER = {
    rule["id"]: ghost["id"] for ghost in GHOSTS for rule in ghost.get("rules", [])
}


def _dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_immutable_pack(pack_dir: Path, files: Mapping[Path, Any]) -> bool:
    """Create a pack version once, or no-op when the existing bytes match."""
    rendered: dict[Path, bytes] = {}
    for relative_path, data in files.items():
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"pack output path must stay inside version directory: {relative}")
        if relative in rendered:
            raise ValueError(f"duplicate pack output path: {relative}")
        rendered[relative] = (
            json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

    if pack_dir.is_symlink():
        raise ValueError(f"pack version directory cannot be a symlink: {pack_dir}")
    if pack_dir.exists():
        if not pack_dir.is_dir():
            raise ValueError(f"pack version path is not a directory: {pack_dir}")
        existing: dict[Path, bytes] = {}
        for path in pack_dir.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"pack version cannot contain symlinks: {path}")
            if path.is_file():
                existing[path.relative_to(pack_dir)] = path.read_bytes()
        if existing == rendered:
            return False
        raise ValueError(
            f"pack version {pack_dir.parent.name}@{pack_dir.name} is immutable; "
            "bump PACK_VERSION instead of overwriting it"
        )

    pack_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{pack_dir.name}.staging-", dir=pack_dir.parent
    ) as temp_root:
        staged_pack = Path(temp_root) / pack_dir.name
        for relative, content in rendered.items():
            target = staged_pack / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        staged_pack.rename(pack_dir)
    return True


def _effect_refs(effects: Iterable[dict[str, Any]], key: str) -> set[str]:
    return {str(e[key]) for e in effects if isinstance(e, dict) and key in e}


def _collect_item_refs(domain_characters: list[dict[str, Any]], domain_stories: list[dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for character in domain_characters:
        for node in character["choices"]:
            refs |= _effect_refs(node[3], "item_ref")
    for story in domain_stories:
        for node in story["nodes"]:
            refs |= _effect_refs(node[3], "item_ref")
    return refs


def _collect_rule_refs(domain_characters: list[dict[str, Any]], domain_stories: list[dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for character in domain_characters:
        for node in character["choices"]:
            refs |= _effect_refs(node[3], "rule_ref")
    for story in domain_stories:
        for node in story["nodes"]:
            refs |= _effect_refs(node[3], "rule_ref")
    return refs


def _node_dict(node: tuple[str, str, list[str], str, str, str]) -> dict[str, Any]:
    nid, name, tags, clue, light, distance = node
    return {
        "id": nid,
        "name": name,
        "tags": tags,
        "environment": {"light": light, "distance": distance},
        "clues": [clue],
    }


def _encounter_dicts(domain: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for eid, location, refs in domain["encounters"]:
        result.append(
            {
                "id": eid,
                "location_ref": location,
                "enemy_refs": [{"ghost_ref": gid, "quantity": qty} for gid, qty in refs],
            }
        )
    return result


def _hard_encounters(domain: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {eid: (location, refs) for eid, location, refs in domain["encounters"]}
    result = []
    for eid in domain["hard_extra"]:
        if eid not in by_id:
            continue
        location, refs = by_id[eid]
        result.append(
            {
                "id": f"{eid}_hard",
                "location_ref": location,
                "enemy_refs": [{"ghost_ref": gid, "quantity": qty} for gid, qty in refs],
            }
        )
    return result


def _goal_dicts(domain: dict[str, Any]) -> list[dict[str, Any]]:
    boss_ghost = domain["encounters"][0][2][0][0] if domain["encounters"] else ""
    boss_name = _GHOSTS.get(boss_ghost, {}).get("name", "首领")
    last_node = domain["nodes"][-1][0]
    return [
        {
            "id": f"{domain['id']}_rule",
            "description": f"发现{boss_name}的杀人规律。",
            "conditions": [{"type": "rule_discovered", "rule_ref": domain["boss_rule"]}],
        },
        {
            "id": f"{domain['id']}_clues",
            "description": "调查至少 2 条地点线索。",
            "conditions": [{"type": "counter_at_least", "counter": "clues_found", "value": 2}],
        },
        {
            "id": f"{domain['id']}_exit",
            "description": "抵达撤离点。",
            "conditions": [{"type": "location_is", "location_ref": last_node}],
        },
    ]


def _domain_dict(domain: dict[str, Any]) -> dict[str, Any]:
    last_node = domain["nodes"][-1][0]
    record: dict[str, Any] = {
        "id": domain["id"],
        "schema_version": 1,
        "name": domain["name"],
        "aliases": [domain["name"].split("·")[-1]],
        "description": domain["description"],
        "chapter": domain["chapter"],
        "supported_modes": ["solo", "party"],
        "party_size": {"min": 2, "max": 4},
        "mission_kit": [{"item_ref": item, "quantity": 1} for item in domain["kit"]],
        "nodes": [_node_dict(node) for node in domain["nodes"]],
        "encounters": _encounter_dicts(domain),
        "goals": _goal_dicts(domain),
        "difficulty_levels": {
            "normal": {
                "unlocked_by_default": True,
                "minimum_party_actions": int(domain.get("min_party_actions", 2)),
                "rewards": [{"type": "resource_reward", "resource": "soul_shards", "amount": 2}],
                "unlocks": ["hard"],
            },
            "hard": {
                "unlocked_by_default": False,
                "minimum_party_actions": 2,
                "rewards": [{"type": "resource_reward", "resource": "soul_shards", "amount": 4}],
                "unlocks": [],
            },
        },
        "difficulty_variants": {
            "hard": {
                "mission_kit_remove": [],
                "extra_encounters": _hard_encounters(domain),
                "enemy_behavior_overrides": {},
            }
        },
        "unlock": {
            "optional": domain["optional"],
            "requires_completed": [],
            "unlocks": domain["unlocks"],
            "requires_clues": int(domain.get("requires_clues", 0) or 0),
        },
        "extraction": {"location_ref": last_node},
    }
    if domain["ending"]:
        record["ending"] = {"text": domain["ending"], "title": "第四种存在"}
    return record


def _character_dict(character: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": character["id"],
        "schema_version": 1,
        "name": character["name"],
        "description": character["description"],
        "domain_ref": character["domain"],
        "location_ref": character["location"],
        "dialogue_nodes": [
            {
                "id": f"{character['id']}_first",
                "text": character["text"],
                "conditions": [],
                "choices": [
                    {
                        "id": choice[0],
                        "label": choice[1],
                        "text": choice[2],
                        "effects": choice[3],
                        **({"conditions": choice[4]} if len(choice) > 4 else {}),
                    }
                    for choice in character["choices"]
                ],
            }
        ],
    }


def _story_dict(story: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": story["id"],
        "schema_version": 1,
        "name": story["name"],
        "description": story["description"],
        "domain_ref": story["domain"],
        "trigger": {"conditions": []},
        "nodes": [
            {
                "id": nid,
                "location_ref": location,
                "text": text,
                "once": True,
                "conditions": conditions,
                "effects": effects,
            }
            for nid, location, text, effects, conditions in story["nodes"]
        ],
    }


def build() -> list[Path]:
    packs_root = Path(__file__).resolve().parents[1] / "content" / "packs"
    written: list[Path] = []
    order = [d["id"] for d in DOMAINS if not d["optional"]]

    for domain in DOMAINS:
        if domain["id"] == "seven_school":
            continue
        pack_dir = packs_root / domain["id"] / PACK_VERSION

        domain_characters = [c for c in CHARACTERS if c["domain"] == domain["id"]]
        domain_stories = [s for s in STORIES if s["domain"] == domain["id"]]

        ghost_ids: list[str] = []
        for _eid, _location, refs in domain["encounters"]:
            for gid, _qty in refs:
                if gid not in ghost_ids:
                    ghost_ids.append(gid)
        if not ghost_ids:
            raise ValueError(f"{domain['id']} has no encounter ghosts")

        item_ids: list[str] = [i for i in domain["kit"]]
        for ref in sorted(_collect_item_refs(domain_characters, domain_stories)):
            if ref not in item_ids:
                item_ids.append(ref)

        rule_owners: list[str] = []
        for rule_id in sorted(_collect_rule_refs(domain_characters, domain_stories)):
            owner = _RULE_OWNER.get(rule_id)
            if owner and owner not in ghost_ids and owner not in rule_owners:
                rule_owners.append(owner)
        ghost_ids.extend(rule_owners)

        if domain["id"] == DEEP_PACK:
            ghost_ids = [g["id"] for g in GHOSTS]
            item_ids = [i["id"] for i in ITEMS]

        missing = [g for g in ghost_ids if g not in _GHOSTS]
        if missing:
            raise ValueError(f"{domain['id']} references unknown ghosts: {missing}")
        missing_items = [i for i in item_ids if i not in _ITEMS]
        if missing_items:
            raise ValueError(f"{domain['id']} references unknown items: {missing_items}")

        domain_record = _domain_dict(domain)
        if not domain["optional"]:
            position = order.index(domain["id"])
            domain_record["unlock"]["requires_completed"] = (
                ["seven_school"] if position == 0 else [order[position - 1]]
            )

        manifest = {
            "pack_id": domain["id"],
            "version": PACK_VERSION,
            "schema_version": 1,
            "requires_engine_version": ENGINE_VERSION,
            "name": domain["name"],
            "description": domain["description"],
            "chapter": domain["chapter"],
            "optional": domain["optional"],
            "starter_ghost_pool": [{"ghost_ref": ghost_ids[0], "weight": 1}],
        }

        files: dict[Path, Any] = {
            Path("manifest.json"): manifest,
            Path("domains") / f"{domain['id']}.json": domain_record,
        }
        for gid in ghost_ids:
            files[Path("ghosts") / f"{gid}.json"] = _GHOSTS[gid]
        for iid in item_ids:
            files[Path("items") / f"{iid}.json"] = _ITEMS[iid]
        for character in domain_characters:
            files[Path("characters") / f"{character['id']}.json"] = _character_dict(character)
        for story in domain_stories:
            files[Path("stories") / f"{story['id']}.json"] = _story_dict(story)

        _write_immutable_pack(pack_dir, files)
        written.append(pack_dir)
    return written


if __name__ == "__main__":
    packs = build()
    print(f"built {len(packs)} pack(s)")
    for pack in packs:
        print(" -", pack.parent.name, pack.name)
