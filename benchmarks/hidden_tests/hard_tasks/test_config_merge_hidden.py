from config_merge import deep_merge


def test_result_is_detached_from_both_inputs() -> None:
    base = {"service": {"timeout": 10}, "regions": ["us"]}
    override = {"service": {"retries": 3}}

    result = deep_merge(base, override)
    result["service"]["timeout"] = 99
    result["regions"].append("eu")

    assert base == {"service": {"timeout": 10}, "regions": ["us"]}
    assert override == {"service": {"retries": 3}}


def test_merge_recurses_through_multiple_levels() -> None:
    assert deep_merge(
        {"a": {"b": {"left": 1}}},
        {"a": {"b": {"right": 2}}},
    ) == {"a": {"b": {"left": 1, "right": 2}}}
