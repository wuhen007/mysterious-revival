from __future__ import annotations

import sys
from pathlib import Path

from ..content_registry import ContentRegistry


def main() -> int:
    plugin_root = Path(__file__).resolve().parents[1]
    packs_root = plugin_root / "content" / "packs"
    registry = ContentRegistry(packs_root)
    issues = registry.load()
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print(f"Content packs valid: {len(registry.manifests)} pack version(s), {len(registry.entries)} object(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
