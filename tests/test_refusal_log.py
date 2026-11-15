from datum.api.refusal_log import RefusalLog

ROUTE = "/v1/logs/ingest"


class Clock:
    """Time a test moves by hand, so no test waits on the wall clock."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_first_refusal_is_logged_at_once():
    assert RefusalLog().count_refusal("acme", ROUTE, 429) == 1


def test_the_next_line_carries_the_refusals_held_back():
    clock = Clock()
    log = RefusalLog(period=60, clock=clock)

    assert log.count_refusal("acme", ROUTE, 429) == 1
    assert [log.count_refusal("acme", ROUTE, 429) for _ in range(4)] == [0, 0, 0, 0]

    clock.now = 60
    assert log.count_refusal("acme", ROUTE, 429) == 5


def test_each_sender_route_and_status_is_counted_apart():
    log = RefusalLog()

    assert log.count_refusal("acme", ROUTE, 429) == 1
    assert log.count_refusal("other", ROUTE, 429) == 1
    assert log.count_refusal("acme", "/v1/ingest", 429) == 1
    assert log.count_refusal("acme", ROUTE, 422) == 1
