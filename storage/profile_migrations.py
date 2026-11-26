"""Versioned, pure migrations for player profile JSON payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

CURRENT_PROFILE_SCHEMA_VERSION = 1


class ProfileMigrationError(ValueError):
    """Raised when a profile cannot be safely migrated without data loss."""


def _migrate_v0_to_v1(payload: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(payload)
    completed_domains = migrated.get("completed_domains", [])
    if not isinstance(completed_domains, list):
        raise ProfileMigrationError("completed_domains must be a list")

    raw_clears = migrated.get("completed_difficulties", {})
    if not isinstance(raw_clears, dict):
        raise ProfileMigrationError("completed_difficulties must be an object")
    completed_difficulties = dict(raw_clears)
    for domain_id, difficulties in completed_difficulties.items():
        if not isinstance(difficulties, list):
            raise ProfileMigrationError(
                f"completed_difficulties[{domain_id!r}] must be a list"
            )
    for domain_id in completed_domains:
        completed_difficulties.setdefault(str(domain_id), ["normal"])

    migrated["completed_difficulties"] = completed_difficulties
    return migrated


_MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    1: _migrate_v0_to_v1,
}


def migrate_profile_payload(payload: Any) -> dict[str, Any]:
    """Return a migrated copy; preserve unknown keys and never mutate input."""
    if not isinstance(payload, Mapping):
        raise ProfileMigrationError("profile payload must be an object")

    migrated = dict(payload)
    raw_version = migrated.get("profile_schema_version", 0)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise ProfileMigrationError("profile_schema_version must be an integer")
    if raw_version < 0:
        raise ProfileMigrationError("profile_schema_version cannot be negative")
    if raw_version > CURRENT_PROFILE_SCHEMA_VERSION:
        raise ProfileMigrationError(
            f"profile schema {raw_version} is newer than supported "
            f"{CURRENT_PROFILE_SCHEMA_VERSION}"
        )

    version = raw_version
    while version < CURRENT_PROFILE_SCHEMA_VERSION:
        next_version = version + 1
        migration = _MIGRATIONS.get(next_version)
        if migration is None:
            raise ProfileMigrationError(f"missing profile migration to version {next_version}")
        migrated = migration(migrated)
        migrated["profile_schema_version"] = next_version
        version = next_version
    return migrated
