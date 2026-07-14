"""Pure, time-injected tests for the takeover/release watchdog state machine."""

from ext_ems.gateway import State, Watchdog


def test_startup_is_self_consumption():
    wd = Watchdog(timeout_s=6.0)
    # No external message yet -> safe default before the first tick.
    assert wd.state == State.SELF_CONSUMPTION
    res = wd.tick(now=0.0)
    assert res.state == State.SELF_CONSUMPTION
    assert res.active_source == 0
    assert res.effective_setpoint_kw == 0.0
    assert res.changed is False


def test_fresh_external_follows():
    wd = Watchdog(timeout_s=6.0)
    wd.on_external(-123.0, now=0.0)
    res = wd.tick(now=0.0)
    assert res.state == State.FOLLOWING_EXTERNAL
    assert res.active_source == 1
    assert res.effective_setpoint_kw == -123.0
    assert res.transition == "RELEASE"  # SELF -> FOLLOWING on first fresh msg


def test_boundary_just_below_timeout_still_following():
    wd = Watchdog(timeout_s=6.0)
    wd.on_external(50.0, now=0.0)
    wd.tick(now=0.0)  # enter FOLLOWING
    res = wd.tick(now=6.0 - 1e-6)  # age just under timeout
    assert res.state == State.FOLLOWING_EXTERNAL
    assert res.effective_setpoint_kw == 50.0
    assert res.changed is False


def test_at_timeout_takes_over():
    wd = Watchdog(timeout_s=6.0)
    wd.on_external(50.0, now=0.0)
    wd.tick(now=0.0)
    res = wd.tick(now=6.0)  # age == timeout -> stale -> takeover
    assert res.state == State.SELF_CONSUMPTION
    assert res.active_source == 0
    assert res.effective_setpoint_kw == 0.0
    assert res.transition == "TAKEOVER"
    assert wd.takeover_count == 1


def test_release_on_fresh_message_after_takeover():
    wd = Watchdog(timeout_s=6.0)
    wd.on_external(50.0, now=0.0)
    wd.tick(now=0.0)
    wd.tick(now=6.0)  # takeover
    wd.on_external(-200.0, now=10.0)  # external returns
    res = wd.tick(now=10.0)
    assert res.state == State.FOLLOWING_EXTERNAL
    assert res.effective_setpoint_kw == -200.0
    assert res.transition == "RELEASE"


def test_transition_fires_once_idempotent():
    wd = Watchdog(timeout_s=6.0)
    wd.on_external(50.0, now=0.0)
    wd.tick(now=0.0)
    first = wd.tick(now=6.0)  # takeover fires
    second = wd.tick(now=7.0)  # still stale, no new transition
    third = wd.tick(now=8.0)
    assert first.transition == "TAKEOVER"
    assert second.transition is None and second.changed is False
    assert third.transition is None
    assert wd.takeover_count == 1


def test_self_consumption_kw_is_configurable():
    wd = Watchdog(timeout_s=6.0, self_consumption_kw=12.5)
    res = wd.tick(now=0.0)
    assert res.effective_setpoint_kw == 12.5


def test_age_reported():
    wd = Watchdog(timeout_s=6.0)
    assert wd.last_external_age_s(now=5.0) is None
    wd.on_external(0.0, now=2.0)
    assert wd.last_external_age_s(now=5.0) == 3.0
