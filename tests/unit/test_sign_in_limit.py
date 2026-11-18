"""R14: wrong guesses are slowed; the owner is never locked out."""

from app.api.sessions import LoginThrottle


def test_wrong_guesses_from_one_address_are_slowed() -> None:
    throttle = LoginThrottle(limit=5, overall=50)
    for _ in range(5):
        throttle.fail("10.0.0.7")
    assert throttle.blocked("10.0.0.7")
    assert not throttle.blocked("10.0.0.8"), "another address is counted on its own"


def test_many_addresses_together_reach_the_overall_ceiling() -> None:
    throttle = LoginThrottle(limit=5, overall=8)
    for index in range(8):
        throttle.fail(f"10.0.0.{index}")
    assert throttle.blocked("10.0.0.99")


def test_failures_age_out() -> None:
    now = [100.0]
    throttle = LoginThrottle(limit=2, overall=50, window=60.0, clock=lambda: now[0])
    throttle.fail("a")
    throttle.fail("a")
    assert throttle.blocked("a")
    now[0] += 61
    assert not throttle.blocked("a")
