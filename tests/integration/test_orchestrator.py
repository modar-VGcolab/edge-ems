"""Core orchestrator cycle tests (wiring of tasks 13-16, 24, 25).

Uses the real InfluxWriter (with a fake write_api), real dispatcher and
watchdog, and fake Modbus adapters. Drives coroutines with asyncio.run() per
the codebase convention.
"""

import asyncio
import time

from common.points import GOOD, PointValue
from core.adapters.base import WriteResult
from core.dispatcher import SetpointDispatcher
from core.influx_writer import InfluxWriter
from core.orchestrator import Orchestrator
from core.watchdog import SetpointWatchdog


class FakeAdapter:
    def __init__(self, values):
        self._values = values
        self.writes = []

    async def read_points(self, names):
        ts = time.time()
        return {n: PointValue(self._values.get(n, 0.0), ts, GOOD) for n in names}

    async def write_points(self, values):
        self.writes.append(dict(values))
        return WriteResult(ok=True)


class FakeWriteApi:
    def __init__(self):
        self.records = []

    def write(self, bucket=None, org=None, record=None):
        self.records.extend(record if isinstance(record, list) else [record])


def _setup(dm):
    adapters = {
        "bess-01": FakeAdapter(
            {
                "soc_pct": 55.0,
                "active_power_kw": -100.0,
                "available_charge_power_kw": 400.0,
                "available_discharge_power_kw": 600.0,
            }
        ),
        "pcc-01": FakeAdapter({"active_power_kw": 120.0, "frequency_hz": 50.0, "voltage_v": 230.0}),
    }
    classes = {"bess-01": "battery", "pcc-01": "pcc"}
    input_points = {
        "bess-01": list(dm.asset_classes["battery"].points_by_direction("input")),
        "pcc-01": list(dm.asset_classes["pcc"].points_by_direction("input")),
    }
    api = FakeWriteApi()
    writer = InfluxWriter(dm, "site-1", bucket="edge_ems", org="edge", write_api=api)
    dispatcher = SetpointDispatcher(dm, "site-1", adapters, classes, max_age_s=5.0)
    watchdog = SetpointWatchdog(adapters, classes, period_s=1.0, max_silence_cycles=2)
    orch = Orchestrator(
        dm,
        "site-1",
        adapters,
        classes,
        input_points,
        writer,
        dispatcher,
        watchdog,
        weights={"bess-01": 2000.0},
    )
    return orch, adapters, api, watchdog


def _payload(now, seq=1):
    from datetime import datetime, timezone

    return {
        "data_model_version": "0.1",
        "site_id": "site-1",
        "ts": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "seq": seq,
        "asset_class": "battery",
        "setpoints": {"active_power_setpoint_kw": -300.0, "reactive_power_setpoint_kvar": 0.0},
    }


def test_poll_cycle_writes_asset_and_aggregate(dm):
    orch, _, api, _ = _setup(dm)
    written = asyncio.run(orch.poll_cycle(time.time()))
    measurements = [p._name for p in api.records]
    assert "battery" in measurements  # per-asset battery
    assert "pcc" in measurements  # per-asset pcc
    assert "aggregate" in measurements  # aggregated battery
    assert written == len(api.records)


def test_handle_setpoint_dispatches_and_feeds_watchdog(dm):
    orch, adapters, _, watchdog = _setup(dm)
    now = time.time()
    asyncio.run(orch.poll_cycle(now))  # populate latest readings for unit limits
    report = asyncio.run(orch.handle_setpoint(_payload(now), now))
    assert report.accepted
    assert adapters["bess-01"].writes  # setpoint reached the device
    assert not watchdog.is_silent(now + 1.0)  # watchdog was fed


def test_safety_cycle_trips_when_silent(dm):
    orch, adapters, _, watchdog = _setup(dm)
    watchdog.start(0.0)
    assert not asyncio.run(orch.safety_cycle(1.0))  # within window
    assert asyncio.run(orch.safety_cycle(5.0))  # silent -> trip
    # battery driven to safe-state (0 kW)
    assert adapters["bess-01"].writes[-1]["active_power_setpoint_kw"] == 0.0


def test_setpoint_clears_then_silence_retrips(dm):
    orch, adapters, _, watchdog = _setup(dm)
    now = time.time()
    asyncio.run(orch.poll_cycle(now))
    asyncio.run(orch.handle_setpoint(_payload(now), now))
    # after a setpoint, a short gap must not trip
    assert not asyncio.run(orch.safety_cycle(now + 1.0))
    # but a long silence does
    assert asyncio.run(orch.safety_cycle(now + 10.0))


def test_battery_unit_limits_from_readings(dm):
    orch, _, _, _ = _setup(dm)
    asyncio.run(orch.poll_cycle(time.time()))
    limits = orch._battery_unit_limits()
    assert limits["bess-01"].charge_kw == 400.0
    assert limits["bess-01"].discharge_kw == 600.0
