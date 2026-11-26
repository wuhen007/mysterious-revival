from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any


def observe(
    rule: Mapping[str, Any],
    progress: MutableMapping[str, Any],
    current_san: int,
    already_known: bool = False,
) -> tuple[str, bool, int]:
    if already_known or progress.get("discovered"):
        return (f"你已掌握规律：{rule.get('counter_method', '结合线索应对其行为。')}", False, current_san)
    clues = rule.get("clues", [])
    if not isinstance(clues, list) or not clues:
        return ("暂时没有发现新的线索。", False, current_san)
    index = max(0, int(progress.get("clue_index", 0)))
    if index >= len(clues):
        return ("没有新的线索；继续观察或尝试已知反制。", False, current_san)
    clue = str(clues[index])
    progress["clue_index"] = index + 1
    reveal_after = max(1, int(rule.get("reveal_after", len(clues))))
    discovered = index + 1 >= reveal_after
    if discovered:
        progress["discovered"] = True
        current_san = max(0, current_san - int(rule.get("discovery_san_cost", 5)))
    return clue, discovered, current_san


def reveal_from_counteraction(
    rule: Mapping[str, Any], progress: MutableMapping[str, Any], current_san: int
) -> tuple[bool, int]:
    if progress.get("discovered"):
        return False, current_san
    progress["discovered"] = True
    current_san = max(0, current_san - int(rule.get("discovery_san_cost", 5)))
    return True, current_san
