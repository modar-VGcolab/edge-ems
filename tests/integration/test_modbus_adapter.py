"""Integration: ModbusTcpAdapter against the simulator — adapter and simulator
share the codec and map files, so this is the full southbound contract."""

import asyncio
import socket
import threading
import time

import pytest
from common.register_map import load_register_map
from core.adapters.base import COMM_FAIL, GOOD
from core.adapters.modbus_tcp import ModbusTcpAdapter
from simulator.device import SimulatedDevice

INPUTS = [
    "soc_pct",
    "active_power_kw",
    "reactive_power_kvar",
    "available_charge_power_kw",
    "available_discharge_power_kw",
]

INITIAL = {
    "soc_pct": 57.5,
    "active_power_kw": -312.5,
    "reactive_power_kvar": 12.5,
    "available_charge_power_kw": 1000.0,
    "available_discharge_power_kw": 800.0,
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def bess_map(repo_root, dm):
    return load_register_map(repo_root / "maps" / "custom_bess_v1.yaml", dm)


@pytest.fixture()
def live_device(bess_map):
    device = SimulatedDevice(bess_map, initial=dict(INITIAL))
    port = _free_port()
    threading.Thread(
        target=lambda: asyncio.run(device.serve("127.0.0.1", port)), daemon=True
    ).start()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        pytest.fail("simulator did not start")
    return device, port


def test_read_points_roundtrip(live_device, bess_map):
    device, port = live_device

    async def scenario():
        adapter = ModbusTcpAdapter("127.0.0.1", bess_map, port=port)
        values = await adapter.read_points(INPUTS)
        await adapter.disconnect()
        return values

    values = asyncio.run(scenario())
    for name, expected in INITIAL.items():
        assert values[name].quality == GOOD
        assert values[name].value == pytest.approx(expected), name


def test_write_points_lands_in_device(live_device, bess_map):
    device, port = live_device

    async def scenario():
        adapter = ModbusTcpAdapter("127.0.0.1", bess_map, port=port)
        result = await adapter.write_points(
            {"active_power_setpoint_kw": -250.0, "reactive_power_setpoint_kvar": 5.0}
        )
        await adapter.disconnect()
        return result

    result = asyncio.run(scenario())
    assert result.ok, result.errors
    assert device.get_point("active_power_setpoint_kw") == pytest.approx(-250.0)
    assert device.get_point("reactive_power_setpoint_kvar") == pytest.approx(5.0)


def test_write_readonly_point_rejected_locally(live_device, bess_map):
    device, port = live_device

    async def scenario():
        adapter = ModbusTcpAdapter("127.0.0.1", bess_map, port=port)
        result = await adapter.write_points({"soc_pct": 99.0})
        await adapter.disconnect()
        return result

    result = asyncio.run(scenario())
    assert not result.ok
    assert "read-only" in result.errors["soc_pct"]
    assert device.get_point("soc_pct") == pytest.approx(57.5)  # untouched


def test_setpoint_enables_asserted_on_connect(live_device, bess_map):
    # connect() alone should latch WSetEna/VarSetEna to 1, so the battery honours
    # the active/reactive setpoints written on later cycles (they are ignored
    # while the enable is 0). No explicit write_points call is made here.
    device, port = live_device

    async def scenario():
        adapter = ModbusTcpAdapter("127.0.0.1", bess_map, port=port)
        await adapter.connect()
        await adapter.disconnect()

    asyncio.run(scenario())
    assert device.get_point("active_power_setpoint_enable") == pytest.approx(1.0)
    assert device.get_point("reactive_power_setpoint_enable") == pytest.approx(1.0)


def test_dead_device_yields_comm_fail(bess_map):
    port = _free_port()  # nothing listening

    async def scenario():
        adapter = ModbusTcpAdapter("127.0.0.1", bess_map, port=port, timeout=0.3)
        values = await adapter.read_points(INPUTS)
        health = adapter.health()
        await adapter.disconnect()
        return values, health

    values, health = asyncio.run(scenario())
    assert all(v.quality == COMM_FAIL for v in values.values())
    assert all(v.value is None for v in values.values())
    assert not health.connected
    assert health.consecutive_failures >= 1


def test_backoff_blocks_immediate_retry(bess_map):
    port = _free_port()

    async def scenario():
        adapter = ModbusTcpAdapter("127.0.0.1", bess_map, port=port, timeout=0.3)
        await adapter.read_points(["soc_pct"])  # first failure starts backoff
        t0 = time.monotonic()
        await adapter.read_points(["soc_pct"])  # inside backoff window: instant
        elapsed = time.monotonic() - t0
        await adapter.disconnect()
        return elapsed

    elapsed = asyncio.run(scenario())
    assert elapsed < 0.2  # no second connection attempt was made
