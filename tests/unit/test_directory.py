from types import SimpleNamespace as NS

from phi_framework.directory import TagPolicy


def test_tag_policy_override_and_api():
    assert TagPolicy(None, "k", override="a, b,,c").allowed == {"a", "b", "c"}
    w = NS(tag_policies=NS(get_tag_policy=lambda key: NS(values=[NS(name="x"), NS(name="y")])))
    assert TagPolicy(w, "k").allowed == {"x", "y"}
