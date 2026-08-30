from config_merge import deep_merge


def test_nested_mappings_are_merged_and_lists_are_replaced() -> None:
    base = {"service": {"timeout": 10, "retries": 2}, "regions": ["us"]}
    override = {"service": {"retries": 5}, "regions": ["eu"]}

    assert deep_merge(base, override) == {
        "service": {"timeout": 10, "retries": 5},
        "regions": ["eu"],
    }
