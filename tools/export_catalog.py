"""Export the latest validated versioned JSON content packs to the catalog."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..content_registry import ContentRegistry
from ..engine.models import ContentEntry

_EFFECT_NAMES = {
    "heal": "治疗",
    "san_change": "SAN",
    "loyalty_change": "忠诚",
    "illuminate": "照明",
    "apply_status": "状态",
    "damage": "伤害",
    "capture_bonus": "捕获加成",
}


def _latest_entries(registry: ContentRegistry, content_type: str) -> list[ContentEntry]:
    latest_versions: dict[str, str] = {}
    for pack_id, version in registry.manifests:
        current = latest_versions.get(pack_id)
        if current is None or ContentRegistry._version_key(version) > ContentRegistry._version_key(current):
            latest_versions[pack_id] = version
    return sorted(
        (
            entry
            for entry in registry.entries.values()
            if entry.content_type == content_type
            and latest_versions.get(entry.pack_id) == entry.version
        ),
        key=lambda entry: (str(entry.data.get("id", "")), entry.pack_id),
    )


def _latest_domains(registry: ContentRegistry) -> list[ContentEntry]:
    latest: dict[str, ContentEntry] = {}
    for entry in _latest_entries(registry, "domains"):
        domain_id = str(entry.data.get("id", ""))
        current = latest.get(domain_id)
        if current is None or ContentRegistry._version_key(entry.version) > ContentRegistry._version_key(current.version):
            latest[domain_id] = entry
    return sorted(
        latest.values(),
        key=lambda entry: (int(entry.data.get("chapter", 999)), str(entry.data.get("id", ""))),
    )


def _catalog_variants(entries: list[ContentEntry]) -> list[tuple[ContentEntry, str]]:
    by_id: dict[str, dict[str, list[ContentEntry]]] = {}
    for entry in entries:
        signature = json.dumps(entry.data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        by_id.setdefault(str(entry.data.get("id", "")), {}).setdefault(signature, []).append(entry)

    rows: list[tuple[ContentEntry, str]] = []
    for object_id in sorted(by_id):
        for signature in sorted(by_id[object_id]):
            copies = sorted(by_id[object_id][signature], key=lambda item: (item.pack_id, item.version))
            representative = copies[0]
            source = f"{representative.pack_id}@{representative.version}"
            if len(copies) > 1:
                source += f"（相同定义共 {len(copies)} 包）"
            rows.append((representative, source))
    return rows


def _encounter_ghost_refs(domain: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for encounter in domain.get("encounters", []):
        variants = encounter.get("mode_variants", {})
        ref_groups = [encounter.get("enemy_refs", [])]
        if isinstance(variants, dict):
            ref_groups.extend(
                variant.get("enemy_refs", [])
                for variant in variants.values()
                if isinstance(variant, dict)
            )
        for group in ref_groups:
            for ref in group:
                ghost_id = str(ref.get("ghost_ref", ""))
                if ghost_id and ghost_id not in refs:
                    refs.append(ghost_id)
    for variant in domain.get("difficulty_variants", {}).values():
        if not isinstance(variant, dict):
            continue
        for encounter in variant.get("extra_encounters", []):
            for ref in encounter.get("enemy_refs", []):
                ghost_id = str(ref.get("ghost_ref", ""))
                if ghost_id and ghost_id not in refs:
                    refs.append(ghost_id)
    return refs


def _ghost_domains(domains: list[ContentEntry]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for entry in domains:
        name = str(entry.data.get("name", entry.data.get("id", "")))
        for ghost_id in _encounter_ghost_refs(entry.data):
            names = mapping.setdefault(ghost_id, [])
            if name not in names:
                names.append(name)
    return mapping


def _boss_name(domain: ContentEntry, registry: ContentRegistry) -> str:
    boss_rule = str(domain.data.get("boss_rule", ""))
    if boss_rule:
        for ghost in registry.list_entries("ghosts", domain.pack_id, domain.version):
            if any(rule.get("id") == boss_rule for rule in ghost.data.get("rules", [])):
                return str(ghost.data.get("name", ghost.data["id"]))
    for ghost_id in _encounter_ghost_refs(domain.data):
        ghost = registry.get("ghosts", ghost_id, domain.pack_id, domain.version)
        if ghost:
            return str(ghost.data.get("name", ghost.data["id"]))
    return "—"


def _effect_text(effects: list[dict[str, Any]]) -> str:
    return "、".join(
        _EFFECT_NAMES.get(str(effect.get("type", "")), str(effect.get("type", "")))
        for effect in effects
    )


def build(registry: ContentRegistry) -> str:
    domains = _latest_domains(registry)
    ghost_entries = _latest_entries(registry, "ghosts")
    item_entries = _latest_entries(registry, "items")
    ghosts = _catalog_variants(ghost_entries)
    items = _catalog_variants(item_entries)
    ghost_count = len({entry.data["id"] for entry in ghost_entries})
    item_count = len({entry.data["id"] for entry in item_entries})
    character_entries = _latest_entries(registry, "characters")
    characters = _catalog_variants(character_entries)
    character_count = len({entry.data["id"] for entry in character_entries})
    stories = _latest_entries(registry, "stories")
    domain_names = {
        str(entry.data["id"]): str(entry.data.get("name", entry.data["id"]))
        for entry in domains
    }
    main = [entry for entry in domains if not entry.data.get("unlock", {}).get("optional", False)]
    optional = [entry for entry in domains if entry.data.get("unlock", {}).get("optional", False)]
    lines: list[str] = ["# 正典清单（自动生成）", ""]
    lines.append("> 由 `tools/export_catalog.py` 从当前已加载的版本化 JSON 内容包生成，请勿手改。")
    lines.append("> 同一鬼怪/道具/人物 ID 在不同包中的定义若有差异，会分行标注来源；字节内容相同的定义会合并计数。")
    lines.append("")
    lines.append(
        f"规模：主线 **{len(main)}** 章 · 可选副本 **{len(optional)}** 个 · 鬼怪 **{ghost_count}** 种/{len(ghosts)} 条包内定义 · "
        f"物品 **{item_count}** 种/{len(items)} 条包内定义 · 人物 **{character_count}** 位/{len(characters)} 条包内定义 · "
        f"地点事件 **{len(stories)}** 个"
    )
    lines.append("")

    lines.extend(
        [
            "## 一、章节与场景",
            "",
            "| 序 | 章节 | 节点 | 遭遇 | 首领 | 前置 | 解锁下一章 |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for index, entry in enumerate(main, start=1):
        domain = entry.data
        unlock = domain.get("unlock", {})
        nodes = "、".join(str(node.get("name", node.get("id", ""))) for node in domain.get("nodes", [])) or "—"
        requirements = unlock.get("requires_completed", [])
        required_names = "、".join(domain_names.get(str(item), str(item)) for item in requirements)
        required_names = required_names or ("首个章节" if index == 1 else "—")
        next_names = "、".join(domain_names.get(str(item), str(item)) for item in unlock.get("unlocks", [])) or "—"
        lines.append(
            f"| {index} | {domain.get('name', domain['id'])} | {nodes} | {len(domain.get('encounters', []))} | "
            f"{_boss_name(entry, registry)} | {required_names} | {next_names} |"
        )
    lines.extend(["", "### 可选副本", "", "| 副本 | 节点 | 首领 | 说明 |", "|---|---|---|---|"])
    for entry in optional:
        domain = entry.data
        nodes = "、".join(str(node.get("name", node.get("id", ""))) for node in domain.get("nodes", [])) or "—"
        lines.append(
            f"| {domain.get('name', domain['id'])} | {nodes} | {_boss_name(entry, registry)} | "
            f"{domain.get('description', '')} |"
        )
    lines.append("")

    appearances = _ghost_domains(domains)
    lines.extend(["## 二、鬼怪总表", "", "| 名称 | 来源定义 | 等级 | HP/攻/防/速 | 规律 | 出现章节 |", "|---|---|---|---|---|---|"])
    for entry, source in ghosts:
        ghost = entry.data
        stats = ghost.get("stats", {})
        rule = (ghost.get("rules") or [{}])[0].get("description", "—")
        where = "、".join(appearances.get(str(ghost["id"]), [])) or "档案收录"
        lines.append(
            f"| {ghost.get('name', ghost['id'])} | {source} | {ghost.get('grade', '—')} | "
            f"{stats.get('hp', '—')}/{stats.get('attack', '—')}/{stats.get('defense', '—')}/{stats.get('speed', '—')} | "
            f"{rule} | {where} |"
        )
    lines.append("")

    lines.extend(["## 三、灵异物品总表", "", "| 物品 | 来源定义 | 可用目标 | 效果 | 说明 |", "|---|---|---|---|---|"])
    for entry, source in items:
        item = entry.data
        lines.append(
            f"| {item.get('name', item['id'])} | {source} | {'/'.join(item.get('target_types', []))} | "
            f"{_effect_text(item.get('effects', []))} | {item.get('description', '')} |"
        )
    lines.append("")

    lines.extend(["## 四、人物表", "", "| 人物 | 来源定义 | 所在章节 | 作用 | 首次对话 |", "|---|---|---|---|---|"])
    for entry, source in characters:
        character = entry.data
        dialogue = (character.get("dialogue_nodes") or [{}])[0].get("text", "")
        domain_id = str(character.get("domain_ref", ""))
        lines.append(
            f"| {character.get('name', character['id'])} | {source} | {domain_names.get(domain_id, domain_id)} | "
            f"{character.get('description', '')} | {dialogue} |"
        )
    lines.append("")

    pending = [
        (str(entry.data.get("name", entry.data["id"])), str(entry.data.get("description", "")))
        for entry, _source in [*ghosts, *items]
        if "待补充" in str(entry.data.get("description", ""))
    ]
    lines.extend(["## 五、待补充清单", "", "| 条目 | 缺失内容 |", "|---|---|"])
    lines.extend(f"| {name} | {description} |" for name, description in pending)
    if not pending:
        lines.append("| — | 当前已加载包中的鬼怪和物品均未标注待补充说明。 |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    plugin_root = Path(__file__).resolve().parents[1]
    registry = ContentRegistry(plugin_root / "content" / "packs")
    issues = registry.load()
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    target = plugin_root / "docs" / "正典清单.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build(registry), encoding="utf-8")
    print(f"written {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())