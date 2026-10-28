"""Lookups against the governed-tag policy."""

from __future__ import annotations

from phi_framework.runtime import log


class TagPolicy:
    """Allowed values for the governed tag, read once per run from the Tag Policy API."""

    def __init__(self, workspace_client, tag_key: str, override: str = ""):
        self.tag_key = tag_key
        if override:
            log.warning("Using allowed_values_override instead of the governed-tag policy (dev only)")
            self.allowed: frozenset[str] = frozenset(v.strip() for v in override.split(",") if v.strip())
        else:
            policy = workspace_client.tag_policies.get_tag_policy(tag_key)
            self.allowed = frozenset(v.name for v in (policy.values or []) if v.name)
        if not self.allowed:
            raise RuntimeError(
                f"Governed tag {tag_key!r} has no allowed values; the framework requires a value list."
            )
        log.info("Governed tag %s has %d allowed values", tag_key, len(self.allowed))

    def is_allowed(self, value: str | None) -> bool:
        return value is not None and value in self.allowed
