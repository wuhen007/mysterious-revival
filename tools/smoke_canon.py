"""命令行烟测：不起 AstrBot，直接跑通「建档 → 章节 → 开局 → 探索 → 战斗」链路。

用法（在 E:/AstrBot/data/plugins 下）：
    python -m astrbot_plugin_ghost_domain.tools.smoke_canon
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..content_registry import ContentRegistry
from ..engine.models import Identity
from ..handlers.commands import CommandHandler
from ..modes.party_run import PartyRunService
from ..modes.solo_run import SoloRunService
from ..storage.repository import GameRepository


def _line(text: str, limit: int = 160) -> str:
    flat = " / ".join(part for part in text.splitlines() if part.strip())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def main() -> int:
    plugin_root = Path(__file__).resolve().parents[1]
    registry = ContentRegistry(plugin_root / "content" / "packs")
    issues = registry.load()
    if issues:
        for issue in issues:
            print("ISSUE", issue)
        return 1

    domains = [entry for entry in registry.entries.values() if entry.content_type == "domains"]
    ghosts = [entry for entry in registry.entries.values() if entry.content_type == "ghosts"]
    items = [entry for entry in registry.entries.values() if entry.content_type == "items"]
    characters = [entry for entry in registry.entries.values() if entry.content_type == "characters"]
    print(f"内容：{len(registry.manifests)} 包 / 域 {len(domains)} / 鬼 {len(ghosts)} / 物 {len(items)} / 人 {len(characters)}")

    with tempfile.TemporaryDirectory() as temp:
        repository = GameRepository(Path(temp) / "smoke.sqlite3")
        solo = SoloRunService(repository, registry)
        party = PartyRunService(repository, registry)
        handler = CommandHandler(repository, registry, solo, party)
        identity = Identity("smoke", "aiocqhttp", "u1", "", "m1")
        counter = {"n": 1}

        def fresh() -> Identity:
            counter["n"] += 1
            return Identity("smoke", "aiocqhttp", "u1", "", f"m{counter['n']}")

        print("\n[1] 招募：", _line(handler.recruit(identity).text))
        print("\n[2] 章节：\n" + handler.chapters(identity).text)
        print("\n[3] 未解锁章节：", _line(handler.open_run(identity, "黄岗村 单人 普通").text))
        print("\n[4] 开局：", _line(handler.open_run(identity, "七中学校 单人 普通").text))
        for step in range(3):
            text = handler.forward(fresh()).text
            print(f"\n[5.{step + 1}] 前进：", _line(text))
        print("\n[6] 存档：\n" + handler.save(identity).text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
