"""长线成长：章节解锁、复苏值、代号评定、结局。

全部为纯函数，不依赖 AstrBot，便于单测与模拟。
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

RANKS: tuple[tuple[str, int], ...] = (("S", 13), ("A", 9), ("B", 5), ("C", 0))
DEFAULT_RECOVERY_THRESHOLD = 100
LEGACY_NAMES: dict[str, str] = {
    "legacy_erase": "抹除（张洞）",
    "legacy_gate": "关门（罗文松）",
    "legacy_bury": "埋葬（罗千）",
    "legacy_cut": "切割（李庆之）",
    "legacy_replace": "替换（孟小董）",
    "legacy_copy": "复制（张幼红）",
    "legacy_suppress": "压制（张伯华）",
}


def domain_available(profile_payload: Mapping[str, Any], domain: Mapping[str, Any]) -> bool:
    """主线按前置章节解锁；势力副本（optional）永远可进。"""
    unlock = domain.get("unlock", {})
    required_clues = int(unlock.get("requires_clues", 0) or 0)
    if required_clues and len(profile_payload.get("legacy_clues", [])) < required_clues:
        return False
    if unlock.get("optional", False):
        return True
    if int(domain.get("chapter", 1)) <= 1:
        return True
    domain_id = str(domain.get("id", ""))
    if domain_id in profile_payload.get("unlocked_domains", []):
        return True
    required = unlock.get("requires_completed", [])
    completed = profile_payload.get("completed_domains", [])
    return bool(required) and all(item in completed for item in required)


def unlock_requirements(domain: Mapping[str, Any]) -> list[str]:
    return [str(item) for item in domain.get("unlock", {}).get("requires_completed", [])]


def apply_completion(
    profile_payload: MutableMapping[str, Any],
    domain: Mapping[str, Any],
    outcome: str,
) -> dict[str, Any]:
    """通关后写入章节进度与结局；返回本次新增的解锁项。"""
    unlocked: list[str] = []
    if outcome != "completed":
        return {"unlocked_domains": unlocked, "ending": None}

    domain_id = str(domain.get("id", ""))
    completed = profile_payload.setdefault("completed_domains", [])
    if domain_id and domain_id not in completed:
        completed.append(domain_id)

    opened = profile_payload.setdefault("unlocked_domains", [])
    for next_domain in domain.get("unlock", {}).get("unlocks", []):
        value = str(next_domain)
        if value and value not in opened:
            opened.append(value)
            unlocked.append(value)

    ending = None
    if domain.get("ending"):
        ending = {
            "title": str(domain["ending"].get("title", "结局")),
            "text": str(domain["ending"].get("text", "")),
            "domain_id": domain_id,
        }
        profile_payload["ending"] = ending
        profile_payload["ascended"] = True

    profile_payload["campaign_progress"] = {
        "completed": len(completed),
        "last_domain": domain_id,
        "rank": rank_for(profile_payload),
    }
    return {"unlocked_domains": unlocked, "ending": ending}


def rank_for(profile_payload: Mapping[str, Any]) -> str:
    completed = len(profile_payload.get("completed_domains", []))
    for name, threshold in RANKS:
        if completed >= threshold:
            return name
    return "C"


def next_main_domain(profile_payload: Mapping[str, Any], domains: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for domain in sorted(domains, key=lambda item: int(item.get("chapter", 999))):
        if domain.get("unlock", {}).get("optional"):
            continue
        if domain.get("id") in profile_payload.get("completed_domains", []):
            continue
        if domain_available(profile_payload, domain):
            return domain
    return None


def apply_recovery(
    state: MutableMapping[str, Any],
    profile_id: str,
    amount: int,
    threshold: int = DEFAULT_RECOVERY_THRESHOLD,
) -> tuple[bool, str]:
    """累加复苏值；到达阈值即失控（人死、鬼反噬）。"""
    player = state.get("players", {}).get(profile_id)
    if not isinstance(player, dict) or not player.get("alive", True):
        return False, ""
    current = int(player.get("recovery", 0)) + max(0, int(amount))
    player["recovery"] = min(threshold, current)
    if current < threshold:
        return False, ""
    player["recovery"] = threshold
    player["recovery_collapsed"] = True
    player["hp"] = 0
    player["alive"] = False
    for unit in state.get("ghost_units", []):
        if unit.get("owner_profile_id") == profile_id:
            unit["rebelled"] = True
    state["phase"] = "dead"
    return True, "你的复苏值到达上限——鬼接管了这具身体，本局结束。"


def recovery_stage(value: int) -> str:
    if value >= 100:
        return "失控"
    if value >= 70:
        return "濒临复苏"
    if value >= 40:
        return "明显复苏"
    if value > 0:
        return "轻微复苏"
    return "稳定"
