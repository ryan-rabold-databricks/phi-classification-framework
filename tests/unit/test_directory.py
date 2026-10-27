from types import SimpleNamespace as NS

from phi_framework.directory import GroupDirectory, TagPolicy


def m(kind, value):
    return NS(ref=f"{kind}/{value}", value=value, display=value)


class StubGroups:
    def __init__(self, groups):
        self.groups = groups  # id -> NS(id, display_name, members)

    def list(self, filter, attributes=None):
        name = filter.split('"')[1]
        return [g for g in self.groups.values() if g.display_name == name]

    def get(self, gid):
        return self.groups[gid]


class StubUsers:
    def __init__(self, users):
        self.users = users

    def get(self, uid):
        if uid not in self.users:
            raise RuntimeError("not found")
        return NS(user_name=self.users[uid])


def client():
    groups = {
        "A": NS(id="A", display_name="stewards", members=[m("Users", "u1"), m("Groups", "B"), m("Users", "gone")]),
        "B": NS(id="B", display_name="nested", members=[m("Users", "u2"), m("Groups", "A")]),  # cycle
    }
    return NS(groups=StubGroups(groups), users=StubUsers({"u1": "U1@Example.com", "u2": "u2@example.com"}))


def test_nested_groups_cycles_and_unresolvable_users():
    d = GroupDirectory(client())
    assert d.members("stewards") == {"u1@example.com", "u2@example.com"}
    assert d.is_member(" U2@example.com ", "stewards")
    assert not d.is_member("someone@example.com", "stewards")
    assert not d.is_member(None, "stewards")


def test_unknown_group_is_empty():
    assert GroupDirectory(client()).members("missing") == frozenset()


def test_reviewer_values_resolve_to_group_members_or_one_user():
    d = GroupDirectory(client())
    assert d.principals("stewards") == {"u1@example.com", "u2@example.com"}
    assert d.principals(" Solo@Example.com ") == {"solo@example.com"}
    assert d.principals("not_a_group") == frozenset()
    assert d.principals(None) == frozenset()
    assert d.includes("u2@example.com", "stewards") and d.includes("solo@example.com", "Solo@Example.com")
    assert not d.includes("u1@example.com", "missing")


def test_tag_policy_override_and_api():
    assert TagPolicy(None, "k", override="a, b,,c").allowed == {"a", "b", "c"}
    w = NS(tag_policies=NS(get_tag_policy=lambda key: NS(values=[NS(name="x"), NS(name="y")])))
    assert TagPolicy(w, "k").allowed == {"x", "y"}
