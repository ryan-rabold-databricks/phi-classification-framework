"""Lookups against the governed-tag policy, workspace groups, and scope reviewer values."""

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


class GroupDirectory:
    """Resolves group membership (including nested groups) to lower-case user names.

    Uses the workspace SCIM API, which returns account groups assigned to the
    workspace as well as workspace-local groups. Members are returned as ids, so
    users are resolved individually and cached for the run.
    """

    def __init__(self, workspace_client):
        self.w = workspace_client
        self._members: dict[str, frozenset[str]] = {}
        self._users: dict[str, str | None] = {}
        self._group_exists: dict[str, bool] = {}

    def _user_name(self, user_id: str) -> str | None:
        if user_id not in self._users:
            try:
                self._users[user_id] = (self.w.users.get(user_id).user_name or "").lower() or None
            except Exception as exc:  # deleted user or service principal
                log.debug("Could not resolve user %s: %s", user_id, exc)
                self._users[user_id] = None
        return self._users[user_id]

    def _group_by_name(self, name: str):
        groups = list(self.w.groups.list(filter=f'displayName eq "{name}"', attributes="id,displayName,members"))
        return groups[0] if groups else None

    def members(self, group_name: str) -> frozenset[str]:
        if group_name in self._members:
            return self._members[group_name]
        users: set[str] = set()
        seen: set[str] = set()
        root = self._group_by_name(group_name)
        if root is None:
            log.warning("Group %s not found in this workspace", group_name)
            self._members[group_name] = frozenset()
            return self._members[group_name]
        stack = [root]
        while stack:
            g = stack.pop()
            if g.id in seen:
                continue
            seen.add(g.id)
            full = self.w.groups.get(g.id) if g.members is None else g
            for m in full.members or []:
                ref = m.ref or ""
                if ref.startswith("Groups/"):
                    stack.append(self.w.groups.get(m.value))
                elif ref.startswith("Users/"):
                    name = self._user_name(m.value)
                    if name:
                        users.add(name)
        self._members[group_name] = frozenset(users)
        log.info("Group %s resolved to %d users", group_name, len(users))
        return self._members[group_name]

    def is_member(self, user_name: str | None, group_name: str) -> bool:
        return bool(user_name) and user_name.strip().lower() in self.members(group_name)

    def principals(self, value: str | None) -> frozenset[str]:
        """Users a scope reviewer value stands for.

        ``steward_group`` and ``governance_owner`` each hold one user email or one
        group name. A workspace group with that exact name wins; otherwise a value
        containing ``@`` is a single user. Anything else resolves to no one.
        """
        v = (value or "").strip()
        if not v:
            return frozenset()
        if v not in self._group_exists:
            self._group_exists[v] = self._group_by_name(v) is not None
        if self._group_exists[v]:
            return self.members(v)
        if "@" in v:
            return frozenset({v.lower()})
        log.warning("Reviewer value %s is neither a workspace group nor a user email", v)
        return frozenset()

    def includes(self, user_name: str | None, value: str | None) -> bool:
        """True when ``user_name`` is the user, or a member of the group, named by ``value``."""
        return bool(user_name) and user_name.strip().lower() in self.principals(value)
