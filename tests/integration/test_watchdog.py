"""Setpoint-silence watchdog tests (plan task 25).

Silence detection and trip, re-arming on notify, single-shot trip per episode,
and the safe-state writes to controllable devices (with one failing device not
blocking the rest). Follows the codebase convention of driving coroutines with
asyncio.run() rather than a pytest-asyncio marker.
"""

import asyncio

import pytest
from core.adapters.base import WriteResult
from core.watchdog import DEFAULT_SAFE_SETPOINTS, SetpointWatchdog


class FakeAdapter:
    def __init__(self, fail=False):
        self.fail = fail
        self.writes = []

    async def write_points(self, values: dict) -> WriteResult:
        self.writes.append(dict(values))
        if self.fail:
            raise RuntimeError("device unreachable")
        return WriteResult(ok=True)


def _wd(adapters, classes, period=1.0, cycles=3, safe=None):
    return SetpointWatchdog(
        adapters, classes, period_s=period, max_silence_cycles=cycles, safe_setpoints=safe
    )


def _fleet():
    adapters = {"bess-01": FakeAdapter(), "fload-01": FakeAdapter(), "pv-01": FakeAdapter()}
    classes = {"bess-01": "battery", "fload-01": "flexible_load", "pv-01": "pv"}
    return adapters, classes


# --------------------------------------------------------------- silence logic


def test_not_silent_before_started():
    a, c = _fleet()
    wd = _wd(a, c)
    assert not wd.is_silent(now=1000.0)  # never armed -> no spurious trip


def test_silent_after_timeout():
    a, c = _fleet()
    wd = _wd(a, c, period=1.0, cycles=3)  # 3 s timeout
    wd.start(now=0.0)
    assert not wd.is_silent(now=3.0)  # exactly at the boundary, not yet over
    assert wd.is_silent(now=3.1)


def test_notify_rearms():
    a, c = _fleet()
    wd = _wd(a, c, period=1.0, cycles=3)
    wd.start(now=0.0)
    wd.notify(now=2.0)
    assert not wd.is_silent(now=4.5)  # measured from last notify (2.0), only 2.5 s
    assert wd.is_silent(now=5.1)


# --------------------------------------------------------------- trip behaviour


def test_check_trips_on_silence():
    a, c = _fleet()
    wd = _wd(a, c, period=1.0, cycles=2)
    wd.start(now=0.0)
    assert not asyncio.run(wd.check(now=1.0))  # still within window
    assert asyncio.run(wd.check(now=3.0))  # silent -> trips
    assert wd.tripped
    assert a["bess-01"].writes[-1] == {
        "active_power_setpoint_kw": 0.0,
        "reactive_power_setpoint_kvar": 0.0,
    }


def test_trip_is_single_shot_until_rearmed():
    a, c = _fleet()
    wd = _wd(a, c, period=1.0, cycles=2)
    wd.start(now=0.0)
    asyncio.run(wd.check(now=3.0))  # trips
    assert not asyncio.run(wd.check(now=4.0))  # still silent, already tripped
    assert len(a["bess-01"].writes) == 1  # not re-sent


def test_notify_clears_trip_and_resumes():
    a, c = _fleet()
    wd = _wd(a, c, period=1.0, cycles=2)
    wd.start(now=0.0)
    asyncio.run(wd.check(now=3.0))  # trips
    wd.notify(now=4.0)  # controller speaks again
    assert not wd.tripped
    assert not asyncio.run(wd.check(now=5.0))  # within new window
    assert asyncio.run(wd.check(now=7.0))  # silent again -> trips again
    assert len(a["bess-01"].writes) == 2


# --------------------------------------------------------------- safe-state writes


def test_trip_writes_only_controllable_classes():
    a, c = _fleet()
    wd = _wd(a, c, period=1.0, cycles=1)
    wd.start(now=0.0)
    asyncio.run(wd.check(now=2.0))
    assert a["bess-01"].writes  # battery commanded
    assert a["fload-01"].writes  # flexible load shed
    assert not a["pv-01"].writes  # pv has no default safe-state -> untouched


def test_one_failing_device_does_not_block_others():
    adapters = {"bess-01": FakeAdapter(fail=True), "fload-01": FakeAdapter()}
    classes = {"bess-01": "battery", "fload-01": "flexible_load"}
    wd = _wd(adapters, classes, period=1.0, cycles=1)
    results = asyncio.run(wd.trip())
    assert not results["bess-01"].ok  # captured the failure
    assert results["fload-01"].ok  # other device still commanded
    assert adapters["fload-01"].writes


def test_custom_safe_setpoints():
    a, c = _fleet()
    wd = _wd(a, c, period=1.0, cycles=1, safe={"battery": {"active_power_setpoint_kw": 0.0}})
    asyncio.run(wd.trip())
    assert a["bess-01"].writes[-1] == {"active_power_setpoint_kw": 0.0}
    assert not a["fload-01"].writes  # not in custom map


def test_default_safe_setpoints_cover_battery():
    assert DEFAULT_SAFE_SETPOINTS["battery"]["active_power_setpoint_kw"] == 0.0


def test_rejects_bad_construction():
    a, c = _fleet()
    with pytest.raises(ValueError):
        _wd(a, c, period=0.0)
    with pytest.raises(ValueError):
        _wd(a, c, cycles=0)
