"""Integration tests for the Gateway with fakes (fake controller HTTP via a
capture list, no MQTT/Influx). Asserts the effective setpoint forwarded matches
the state machine across follow -> takeover -> release, and the emulator outage
timeline goes silent in the right window."""

import json

from ext_ems.config import Settings
from ext_ems.emulator import Emulator
from ext_ems.gateway import Gateway, State, Watchdog


class FakeController:
    def __init__(self):
        self.calls = []

    def forward(self, setpoint_kw):
        self.calls.append(setpoint_kw)
        return True

    @property
    def last(self):
        return self.calls[-1] if self.calls else None


def make_gateway(timeout_s=6.0):
    ctrl = FakeController()
    influx_writes = []
    gw = Gateway(Watchdog(timeout_s), ctrl.forward, influx_writes.append)
    return gw, ctrl, influx_writes


def test_follow_takeover_release_sequence():
    gw, ctrl, influx = make_gateway(timeout_s=6.0)

    # follow a fresh external setpoint
    gw.handle_external(-150.0, now=0.0)
    res = gw.tick(now=0.0)
    assert res.state == State.FOLLOWING_EXTERNAL
    assert ctrl.last == -150.0

    # external moves -> forwarded value tracks it
    gw.handle_external(75.0, now=2.0)
    res = gw.tick(now=2.0)
    assert ctrl.last == 75.0

    # silence past the timeout -> takeover to self-consumption (0)
    res = gw.tick(now=8.0)
    assert res.state == State.SELF_CONSUMPTION
    assert res.transition == "TAKEOVER"
    assert ctrl.last == 0.0

    # external returns -> release, follow again
    gw.handle_external(-300.0, now=10.0)
    res = gw.tick(now=10.0)
    assert res.state == State.FOLLOWING_EXTERNAL
    assert res.transition == "RELEASE"
    assert ctrl.last == -300.0

    # influx telemetry written every tick with the active_source flag
    assert influx[0]["active_source"] == 1
    assert any(w["active_source"] == 0 for w in influx)  # the takeover tick
    assert influx[-1]["active_source"] == 1


def test_forward_only_on_change():
    gw, ctrl, _ = make_gateway()
    gw.handle_external(100.0, now=0.0)
    gw.tick(now=0.0)
    gw.tick(now=1.0)  # same setpoint, still fresh -> no new forward
    gw.tick(now=2.0)
    assert ctrl.calls == [100.0]  # forwarded exactly once


def test_status_shape():
    gw, _, _ = make_gateway(timeout_s=6.0)
    gw.handle_external(42.0, now=0.0)
    gw.tick(now=0.0)
    st = gw.status(now=1.0)
    assert st["state"] == "FOLLOWING_EXTERNAL"
    assert st["active_source"] == "external"
    assert st["watchdog_timeout_s"] == 6.0
    assert st["last_pcc_setpoint_kw"] == 42.0
    assert st["takeover_count"] == 0


def test_time_scale_compresses_timings():
    s = Settings(time_scale=60.0, publish_interval_s=300.0, watchdog_timeout_s=360.0)
    assert s.publish_interval == 5.0
    assert s.watchdog_timeout == 6.0
    assert s.outage_at == 10.0  # 600 / 60


def test_emulator_random_in_range_and_silent_in_outage():
    published = []
    emu = Emulator(
        publish=lambda topic, body: published.append((topic, json.loads(body))),
        topic="site/x/external/pcc_setpoint",
        p_min_kw=-300.0,
        p_max_kw=400.0,
        publish_interval_s=5.0,
        outage_at_s=10.0,
        reconnect_at_s=20.0,
    )
    # before outage: publishes within range
    assert emu.publish_tick(elapsed=0.0) is not None
    assert emu.publish_tick(elapsed=5.0) is not None
    for _topic, msg in published:
        assert -300.0 <= msg["pcc_setpoint_kw"] <= 400.0
    # during outage: silent
    assert emu.publish_tick(elapsed=12.0) is None
    # after reconnect: publishes again
    assert emu.publish_tick(elapsed=21.0) is not None
