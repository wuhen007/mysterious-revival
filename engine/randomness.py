from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable, MutableMapping
from typing import Any


def new_rng_state(seed: str | None = None) -> dict[str, Any]:
    return {"seed": seed or secrets.token_hex(16), "counter": 0}


def draw_float(state: MutableMapping[str, Any]) -> float:
    seed = state.get("seed")
    counter = state.get("counter")
    if not isinstance(seed, str) or not seed:
        raise ValueError("RNG state requires a non-empty seed")
    if not isinstance(counter, int) or isinstance(counter, bool) or counter < 0:
        raise ValueError("RNG state counter must be a non-negative integer")
    digest = hashlib.sha256(f"{seed}:{counter}".encode()).digest()
    state["counter"] = counter + 1
    return int.from_bytes(digest[:8], "big") / 2**64


def weighted_choice(
    state: MutableMapping[str, Any], choices: Iterable[tuple[str, float]]
) -> str:
    weighted = [(key, float(weight)) for key, weight in choices if float(weight) > 0]
    if not weighted:
        raise ValueError("weighted choice requires at least one positive weight")
    total = sum(weight for _key, weight in weighted)
    threshold = draw_float(state) * total
    cumulative = 0.0
    for key, weight in weighted:
        cumulative += weight
        if threshold < cumulative:
            return key
    return weighted[-1][0]
