"""Integration: a simulated battery served over real Modbus TCP, read by a real client.

This is the wire-level contract test: if the simulator and a pymodbus client
agree on offsets, types, scaling, and signs, the real adapter will too.
"""

import asyncio
import socket
import threading
import time

import pytest
from common.modbus_codec import holding_offset
from common.register_map import load_register_map
from pymodbus.client import ModbusTcpClient
from simulator.device import SimulatedDevice
from simulator.profiles import ProfileStep, play


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_holding(client: ModbusTcpClient, address: int, count: int):
    try:
        rr = client.read_holding_registers(address, count=count)
    except TypeError:
        rr = client.read_holding_registers(address, count)
    assert not rr.isError(), rr
    return rr.registers


def _write_holding(client: ModbusTcpClient, address: int, values: list[int]):
    rr = client.write_registers(address, values)
    assert not rr.isError(), rr


@pytest.fixture(scope="module")
def bess_map(repo_root):
    from common.data_model import DataModel

    dm = DataModel.load(repo_root / "data_model.yaml")
    return load_register_map(repo_root / "maps" / "custom_bess_v1.yaml", dm)


@pytest.fixture()
def served_device(bess_map):
    device = SimulatedDevice(
        bess_map,
        initial={
            "soc_pct": 57.5,
            "active_power_kw": -312.5,  # discharging: negative per sign convention
            "available_charge_power_kw": 1000.0,
            "available_discharge_power_kw": 800.0,
        },
    )
    port = _free_port()

    def _serve():
        asyncio.run(device.serve("127.0.0.1", port))

    threading.Thread(target=_serve, daemon=True).start()

    client = ModbusTcpClient("127.0.0.1", port=port)
    deadline = time.time() + 5
    while not client.connect():
        if time.time() > deadline:
            pytest.fail("simulator did not start within 5 s")
        time.sleep(0.05)
    yield device, client
    client.close()


def _live_sf(client, reg, bess_map):
    """Read a point's SunSpec scale factor from its served sunssf register."""
    if reg.sf_address is None:
        return None
    from common.modbus_codec import _words_to_int
    return _words_to_int("sunssf", _read_holding(client, holding_offset(reg.sf_address), 1))


def test_client_reads_initial_values(served_device, bess_map):
    device, client = served_device
    from common.modbus_codec import decode

    soc_reg = bess_map.points["soc_pct"]
    words = _read_holding(client, holding_offset(soc_reg.address), soc_reg.width)
    assert decode(soc_reg, words, sf=_live_sf(client, soc_reg, bess_map)) == pytest.approx(57.5)

    p_reg = bess_map.points["active_power_kw"]
    words = _read_holding(client, holding_offset(p_reg.address), p_reg.width)
    assert decode(p_reg, words, sf=_live_sf(client, p_reg, bess_map)) == pytest.approx(-312.5)


def test_client_writes_setpoint(served_device, bess_map):
    device, client = served_device
    from common.modbus_codec import encode

    sp_reg = bess_map.points["active_power_setpoint_kw"]
    _write_holding(client, holding_offset(sp_reg.address),
                   encode(sp_reg, -250.0, sf=_live_sf(client, sp_reg, bess_map)))
    assert device.get_point("active_power_setpoint_kw") == pytest.approx(-250.0)


def test_profile_playback_applies_steps(bess_map):
    device = SimulatedDevice(bess_map, initial={"soc_pct": 50.0})
    steps = [
        ProfileStep(0.0, "soc_pct", 55.0),
        ProfileStep(0.01, "active_power_kw", -100.0),
        ProfileStep(0.02, "soc_pct", 60.0),
    ]
    asyncio.run(play(device, steps, speed=100.0))
    assert device.get_point("soc_pct") == pytest.approx(60.0)
    assert device.get_point("active_power_kw") == pytest.approx(-100.0)
