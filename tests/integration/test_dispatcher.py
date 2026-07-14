"""Integration: full setpoint path — payload guard, power sharing, real Modbus
writes into two simulated batteries plus a derate broadcast to a simulated PV."""

import asyncio
import socket
import threading
import time
from datetime import datetime, timezone

import pytest
from common.register_map import load_register_map
from core.adapters.modbus_tcp import ModbusTcpAdapter
from core.dispatcher import SetpointDispatcher, UnitLimits
from simulator.device import SimulatedDevice


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(device, port):
    threading.Thread(
        target=lambda: asyncio.run(device.serve("127.0.0.1", port)), daemon=True
    ).start()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    pytest.fail("simulator did not start")


def _payload(asset_class, setpoints, seq=1):
    return {
        "data_model_version": "0.1",
        "site_id": "vgcolab-01",
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "seq": seq,
        "asset_class": asset_class,
        "setpoints": setpoints,
    }


@pytest.fixture(scope="module")
def rig(repo_root, dm):
    bess_map = load_register_map(repo_root / "maps" / "custom_bess_v1.yaml", dm)
    pv_map = load_register_map(repo_root / "maps" / "custom_pv_inverter_v1.yaml", dm)
    devices = {
        "bess-01": SimulatedDevice(bess_map),
        "bess-02": SimulatedDevice(bess_map),
        "pv-01": SimulatedDevice(pv_map),
    }
    ports = {}
    for aid, dev in devices.items():
        ports[aid] = _free_port()
        _serve(dev, ports[aid])
    maps = {"bess-01": bess_map, "bess-02": bess_map, "pv-01": pv_map}
    classes = {"bess-01": "battery", "bess-02": "battery", "pv-01": "pv"}
    return devices, ports, maps, classes


def _dispatcher(dm, ports, maps, classes):
    adapters = {aid: ModbusTcpAdapter("127.0.0.1", maps[aid], port=ports[aid]) for aid in ports}
    return SetpointDispatcher(dm, "vgcolab-01", adapters, classes)


LIMITS = {
    "bess-01": UnitLimits(charge_kw=100.0, discharge_kw=100.0),
    "bess-02": UnitLimits(charge_kw=50.0, discharge_kw=50.0),
}


def test_battery_power_shared_into_devices(rig, dm):
    devices, ports, maps, classes = rig
    d = _dispatcher(dm, ports, maps, classes)
    report = asyncio.run(
        d.dispatch(_payload("battery", {"active_power_setpoint_kw": -90.0}, seq=1), LIMITS)
    )
    assert report.accepted and report.leftover_kw == 0.0
    assert all(w.ok for w in report.written.values())
    assert devices["bess-01"].get_point("active_power_setpoint_kw") == pytest.approx(-60.0)
    assert devices["bess-02"].get_point("active_power_setpoint_kw") == pytest.approx(-30.0)


def test_duplicate_seq_rejected(rig, dm):
    _, ports, maps, classes = rig
    d = _dispatcher(dm, ports, maps, classes)
    p = _payload("battery", {"active_power_setpoint_kw": -10.0}, seq=5)
    assert asyncio.run(d.dispatch(p, LIMITS)).accepted
    replay = asyncio.run(d.dispatch(p, LIMITS))
    assert not replay.accepted
    assert "seq" in replay.reason


def test_old_message_rejected(rig, dm):
    _, ports, maps, classes = rig
    d = _dispatcher(dm, ports, maps, classes)
    p = _payload("battery", {"active_power_setpoint_kw": -10.0}, seq=1)
    p["ts"] = "2026-06-12T00:00:00+00:00"  # far in the past
    report = asyncio.run(d.dispatch(p, LIMITS))
    assert not report.accepted
    assert "old" in report.reason


def test_wrong_site_rejected(rig, dm):
    _, ports, maps, classes = rig
    d = _dispatcher(dm, ports, maps, classes)
    p = _payload("battery", {"active_power_setpoint_kw": -10.0})
    p["site_id"] = "someone-elses-site"
    report = asyncio.run(d.dispatch(p, LIMITS))
    assert not report.accepted
    assert "site" in report.reason


def test_derate_broadcast_to_pv(rig, dm):
    devices, ports, maps, classes = rig
    d = _dispatcher(dm, ports, maps, classes)
    report = asyncio.run(d.dispatch(_payload("pv", {"derate_factor_setpoint": 0.8}, seq=1)))
    assert report.accepted
    assert devices["pv-01"].get_point("derate_factor_setpoint") == pytest.approx(0.8, abs=0.001)


def test_reactive_split_equally(rig, dm):
    devices, ports, maps, classes = rig
    d = _dispatcher(dm, ports, maps, classes)
    report = asyncio.run(
        d.dispatch(
            _payload(
                "battery",
                {"active_power_setpoint_kw": -30.0, "reactive_power_setpoint_kvar": 10.0},
                seq=9,
            ),
            LIMITS,
        )
    )
    assert report.accepted
    assert devices["bess-01"].get_point("reactive_power_setpoint_kvar") == pytest.approx(5.0)
    assert devices["bess-02"].get_point("reactive_power_setpoint_kvar") == pytest.approx(5.0)
