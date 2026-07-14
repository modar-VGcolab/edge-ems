import pytest
import yaml
from common.register_map import RegisterMap, load_register_map

ALL_MAPS = [
    "custom_bess_v1.yaml",
    "custom_pv_inverter_v1.yaml",
    "flexible_load_v1.yaml",
    "grid_meter_v1.yaml",
    "meter_v1.yaml",
]


@pytest.mark.parametrize("name", ALL_MAPS)
def test_all_repo_maps_load(name, repo_root, dm):
    rmap = load_register_map(repo_root / "maps" / name, dm)
    assert rmap.points


def test_setpoints_are_writable_in_bess_map(repo_root, dm):
    rmap = load_register_map(repo_root / "maps" / "custom_bess_v1.yaml", dm)
    assert rmap.points["active_power_setpoint_kw"].rw in ("rw", "w")


def test_unknown_point_rejected(repo_root, dm, tmp_path):
    raw = yaml.safe_load((repo_root / "maps" / "custom_bess_v1.yaml").read_text())
    raw["points"]["banana_count"] = {"address": 49999, "type": "uint16"}
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="banana_count"):
        load_register_map(p, dm)


def test_readonly_output_point_rejected(repo_root, dm, tmp_path):
    raw = yaml.safe_load((repo_root / "maps" / "custom_pv_inverter_v1.yaml").read_text())
    raw["points"]["derate_factor_setpoint"]["rw"] = "r"
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="read-only"):
        load_register_map(p, dm)


def test_overlapping_registers_rejected(repo_root, dm, tmp_path):
    raw = yaml.safe_load((repo_root / "maps" / "custom_bess_v1.yaml").read_text())
    # a 32-bit value at 40001 overlaps soc_pct's neighbour at 40002
    raw["points"]["active_power_kw"] = {"address": 40001, "type": "int32", "scale": 0.1}
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="overlaps"):
        load_register_map(p, dm)


def test_width_property():
    raw = {"address": 1, "type": "float32"}
    from common.register_map import RegisterDef

    assert RegisterDef.model_validate(raw).width == 2
    assert RegisterMap  # imported symbol used
