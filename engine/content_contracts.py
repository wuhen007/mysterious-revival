"""Content declarations that currently have runtime handlers."""

SUPPORTED_GHOST_SKILL_TYPES = frozenset({"attack"})
SUPPORTED_GHOST_BEHAVIOR_KINDS = frozenset(
    {"brute", "knock_counter", "dark_stalker", "infection_hunter"}
)

SUPPORTED_STORY_CONDITION_TYPES = frozenset(
    {
        "owns_item",
        "hp_below",
        "rule_discovered",
        "rule_countered",
        "story_flag_matches",
        "difficulty_at_least",
        "counter_at_least",
        "location_is",
    }
)
SUPPORTED_PROFILE_EFFECT_TYPES = frozenset(
    {"unlock_difficulty", "unlock_domain", "add_ghost_slot", "unlock_ability"}
)
SUPPORTED_CAMPAIGN_REWARD_TYPES = frozenset({"resource_reward", "unlock_difficulty"})
SUPPORTED_STORY_EFFECT_TYPES = frozenset(
    {
        "set_story_flag",
        "reveal_rule",
        "item_add",
        "item_remove",
        "resource_reward",
        "unlock_domain",
        "unlock_difficulty",
        "add_ghost_slot",
        "unlock_ability",
        "heal",
        "san_change",
        "damage",
        "increment_counter",
        "move_location",
        "loyalty_change",
    }
)
SUPPORTED_ITEM_EFFECT_TYPES = frozenset(
    {
        "capture_bonus",
        "heal",
        "san_change",
        "loyalty_change",
        "illuminate",
        "apply_status",
        "damage",
    }
)
SUPPORTED_BEHAVIOR_TARGET_STRATEGIES = frozenset(
    {"weakest", "player_first", "ghost_first", "random"}
)
SUPPORTED_BEHAVIOR_DISTANCE_VALUES = frozenset({"close", "far"})

