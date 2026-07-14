"""Every SunSpec asset map is a walkable SunSpec-700 image: a generic client can
walk SunS -> model headers -> end model, and canonical points scale live via SF."""
import pytest
from common.register_map import load_register_map
from simulator.device import SimulatedDevice

_FX_HOLDING = 3

EXPECTED_CHAIN = {
    "custom_bess_v1.yaml": [(1, 66), (701, 153), (704, 65), (713, 7)],
    "custom_pv_inverter_v1.yaml": [(1, 66), (701, 153), (704, 65)],
    "grid_meter_v1.yaml": [(701, 153)],
    "flexible_load_v1.yaml": [(1, 66), (701, 153), (704, 65)],
    "meter_v1.yaml": [(701, 153)],
}
ROUNDTRIP = {
    "custom_bess_v1.yaml": [("soc_pct", 92.0), ("active_power_kw", -400.0),
                            ("active_power_setpoint_kw", 500.0)],
    "custom_pv_inverter_v1.yaml": [("active_power_kw", -250.0), ("derate_factor_setpoint", 0.71)],
    "grid_meter_v1.yaml": [("voltage_v", 230.0), ("frequency_hz", 50.0),
                           ("current_a", 100.0), ("active_power_kw", -1200.0)],
    "flexible_load_v1.yaml": [("active_power_kw", 120.0), ("derate_factor_setpoint", 0.5)],
    "meter_v1.yaml": [("voltage_v", 230.0), ("frequency_hz", 50.0), ("active_power_kw", 150.0)],
}


def _walk(store):
    def rd(off, n):
        return list(store.getValues(_FX_HOLDING, off, n))
    assert rd(0, 2) == [0x5375, 0x6E53]  # 'SunS'
    off, chain = 2, []
    while True:
        mid, length = rd(off, 2)
        if mid == 0xFFFF:
            break
        chain.append((mid, length))
        off += 2 + length
    return chain


@pytest.mark.parametrize("mapfile", list(EXPECTED_CHAIN))
def test_walkable_sunspec_chain(repo_root, dm, mapfile):
    rm = load_register_map(repo_root / "maps" / mapfile, dm)
    dev = SimulatedDevice(rm)
    assert _walk(dev.context[0]) == EXPECTED_CHAIN[mapfile]


@pytest.mark.parametrize("mapfile", list(ROUNDTRIP))
def test_canonical_points_scale_live_via_sf(repo_root, dm, mapfile):
    rm = load_register_map(repo_root / "maps" / mapfile, dm)
    dev = SimulatedDevice(rm)
    for name, value in ROUNDTRIP[mapfile]:
        dev.set_point(name, value)
        assert abs(dev.get_point(name) - value) < 1e-2, (mapfile, name)
