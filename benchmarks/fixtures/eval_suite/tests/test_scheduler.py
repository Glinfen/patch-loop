from scheduler import next_run, retry_delay


def test_scheduler_helpers() -> None:
    assert next_run(10, 5) == 15
    assert retry_delay(3) == 8
