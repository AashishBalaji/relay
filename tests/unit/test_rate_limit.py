from relay.client.rate_limit import RateLimiter


def test_under_limit_never_sleeps():
    sleeps = []
    clock_values = iter([0.0, 0.1, 0.2])
    limiter = RateLimiter(max_calls=5, window_seconds=1.0, clock=lambda: next(clock_values), sleep=sleeps.append)
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()
    assert sleeps == []


def test_hitting_limit_sleeps_for_remaining_window():
    # Two calls allowed per window. Third call within the window must sleep.
    times = iter([0.0, 0.1, 0.1, 1.0])  # 3rd acquire's "now" appears twice (before/after sleep check)
    sleeps = []

    def clock():
        return next(times)

    limiter = RateLimiter(max_calls=2, window_seconds=1.0, clock=clock, sleep=sleeps.append)
    limiter.acquire()  # t=0.0
    limiter.acquire()  # t=0.1
    limiter.acquire()  # t=0.1 -> over limit, must wait until t=1.0 (0.9s), then re-check at t=1.0
    assert sleeps == [0.9]


def test_old_calls_age_out_of_window():
    times = iter([0.0, 2.0])  # second call is 2s later, well outside a 1s window
    sleeps = []
    limiter = RateLimiter(max_calls=1, window_seconds=1.0, clock=lambda: next(times), sleep=sleeps.append)
    limiter.acquire()
    limiter.acquire()
    assert sleeps == []  # the first call already aged out, so no wait needed


def test_rejects_non_positive_max_calls():
    import pytest

    with pytest.raises(ValueError):
        RateLimiter(max_calls=0, window_seconds=1.0)
