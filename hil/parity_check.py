"""Register-parity check (mandatory G3 deliverable, gates every scenario).

Proves that, for every canonical point in every site map, the register
placement is IDENTICAL across three independent realisations:

  1. the register-map file          (maps/*.yaml -- the single source of truth)
  2. the pymodbus simulator         (packages/simulator -- the SIL plant)
  3. the HIL Modbus server          (hil.servers -- the CHIL plant)

For each point it checks the fingerprint (address, type, scale, width, rw) and
then independently *verifies the byte placement*: it encodes a sentinel through
common.modbus_codec, writes it via the device's `set_point`, reads the raw
holding registers back at `holding_offset(address)`, and asserts they equal the
codec's expected words and that `get_point` decodes to the original value. This
catches drift that a fingerprint-only check would miss (e.g. a hand-edited
datastore), which is exactly the silent parity break the prompt warns about.

It also cross-checks the asset_config comm blocks (each asset's register_map and
class) and the SunSpec 40001 holding base.

Exit code 0 = parity holds; 1 = any mismatch. Run before any CHIL scenario:

    python -m hil.parity_check                       # repo defaults
    python -m hil.parity_check --json report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml
from common.config_models import validate_asset_config
from common.data_model import DataModel
from common.modbus_codec import (
    HOLDING_BASE,
    _words_to_int,
    decode,
    encode,
    encode_raw,
    holding_offset,
)
from common.register_map import RegisterDef, RegisterMap, load_register_map
from simulator.device import SimulatedDevice

from hil.plant.models import SiteModel
from hil.servers import HilModbusServer

REPO_ROOT = Path(__file__).resolve().parents[1]
SITE_MAPS = [
    "maps/grid_meter_v1.yaml",
    "maps/custom_bess_v1.yaml",
    "maps/custom_pv_inverter_v1.yaml",
    "maps/flexible_load_v1.yaml",
    "maps/meter_v1.yaml",
]
_FX_HOLDING = 3


@dataclass(frozen=True)
class Fingerprint:
    address: int
    type: str
    scale: float
    width: int
    rw: str

    @classmethod
    def of(cls, reg: RegisterDef) -> Fingerprint:
        return cls(reg.address, reg.type, reg.scale, reg.width, reg.rw)


def _sf_of(rmap: RegisterMap, reg: RegisterDef) -> int | None:
    """Static scale factor a canonical point uses (from its served SF register)."""
    if reg.sf_address is None:
        return None
    sfreg = next(r for r in rmap.points.values() if r.address == reg.sf_address)
    return _words_to_int("sunssf", [(sfreg.value or 0) & 0xFFFF])


def _sentinels(reg: RegisterDef, sf: int | None = None) -> list[float]:
    """A couple of representative engineering values that round-trip cleanly."""
    if reg.type in ("int16", "int32", "float32"):
        raws = [100, -100]
    else:
        raws = [100, 65535 if reg.type == "uint16" else 300000]
    out = []
    for raw in raws:
        v = raw * reg.scale
        try:
            encode(reg, v, sf=sf)  # only keep values that fit the register
            out.append(v)
        except ValueError:
            continue
    return out or [0.0]


def _raw_at(device, offset: int, count: int) -> list[int]:
    """Read raw holding words straight from a device's pymodbus datastore."""
    store = device.context[0]  # single=True -> any unit id returns the device
    return list(store.getValues(_FX_HOLDING, offset, count))


def _verify_placement(label: str, device, rmap: RegisterMap) -> list[str]:
    """Independently confirm each point sits at holding_offset(address) and
    encodes/decodes exactly per the map. `device` exposes get_point/set_point and
    a pymodbus `context`."""
    errors: list[str] = []
    for name, reg in rmap.points.items():
        off = holding_offset(reg.address)
        if reg.role is not None:
            # raw image register (marker/header/sf/sentinel/vendor/end): served verbatim
            expected = encode_raw(reg, reg.value if reg.value is not None else 0)
            raw = _raw_at(device, off, reg.width)
            if raw != expected:
                errors.append(
                    f"{label}: raw register '{name}' at offset {off} = {raw}, "
                    f"expected {expected} (addr {reg.address}, {reg.type})"
                )
            continue
        sf = _sf_of(rmap, reg)
        for value in _sentinels(reg, sf):
            expected = encode(reg, value, sf=sf)
            device.set_point(name, value)
            raw = _raw_at(device, off, reg.width)
            if raw != expected:
                errors.append(
                    f"{label}: '{name}' raw at offset {off} = {raw}, "
                    f"codec expected {expected} (addr {reg.address}, {reg.type})"
                )
            got = device.get_point(name)
            if abs(got - decode(reg, expected, sf=sf)) > 1e-6:
                errors.append(
                    f"{label}: '{name}' decoded {got}, expected ~{decode(reg, expected, sf=sf)}"
                )
    return errors


def _check_overlaps(rmap: RegisterMap) -> list[str]:
    occupied: dict[int, str] = {}
    errors: list[str] = []
    for name, reg in rmap.points.items():
        for o in range(reg.width):
            addr = reg.address + o
            if addr in occupied:
                errors.append(f"'{name}' overlaps '{occupied[addr]}' at {addr}")
            occupied[addr] = name
    return errors


def check_map(map_path: Path, dm: DataModel) -> tuple[list[str], dict]:
    """Full parity check for one map. Returns (errors, fingerprint detail)."""
    errors: list[str] = []
    rmap = load_register_map(map_path, dm)

    # SunSpec 40001 holding base.
    for name, reg in rmap.points.items():
        if reg.address < HOLDING_BASE:
            errors.append(f"{map_path.name}: '{name}' addr {reg.address} below 40001 base")

    errors += [f"{map_path.name}: {e}" for e in _check_overlaps(rmap)]

    # Build the two independent realisations from the SAME map.
    site = SiteModel()
    sim = SimulatedDevice(rmap)
    hil = HilModbusServer(rmap, site, port=0)

    errors += _verify_placement(f"{map_path.name}/simulator", sim, rmap)
    errors += _verify_placement(f"{map_path.name}/hil", hil, rmap)

    # The HIL server's input/setpoint partition must follow the map's rw flags.
    for name in hil.setpoint_points:
        if rmap.points[name].rw == "r":
            errors.append(f"{map_path.name}: '{name}' served as setpoint but map rw=r")
    for name in hil.input_points:
        if rmap.points[name].rw != "r":
            errors.append(f"{map_path.name}: '{name}' served as input but map rw!=r")

    detail = {
        name: asdict(Fingerprint.of(reg)) | {"offset": holding_offset(reg.address)}
        for name, reg in rmap.points.items()
    }
    return errors, detail


def check_asset_config(dm: DataModel) -> list[str]:
    """Every active asset's comm.register_map must exist, parse, and match the
    asset's class -- so the server we stand up is the one core.py will read."""
    errors: list[str] = []
    raw = yaml.safe_load((REPO_ROOT / "configs/asset_config.example.yaml").read_text())
    cfg = validate_asset_config(raw, dm)
    for a in cfg.assets:
        if a.state != "active" or a.comm is None:
            continue
        mp = REPO_ROOT / a.comm.register_map
        if not mp.exists():
            errors.append(f"{a.id}: register_map '{a.comm.register_map}' not found")
            continue
        rmap = load_register_map(mp, dm)
        if rmap.asset_class != a.asset_class:
            errors.append(
                f"{a.id}: map class '{rmap.asset_class}' != asset class '{a.asset_class}'"
            )
    return errors


def run(maps: list[str] | None = None) -> tuple[bool, dict]:
    dm = DataModel.load(REPO_ROOT / "data_model.yaml")
    maps = maps or SITE_MAPS
    all_errors: list[str] = []
    details: dict[str, dict] = {}
    for m in maps:
        errs, detail = check_map(REPO_ROOT / m, dm)
        all_errors += errs
        details[m] = detail
    all_errors += check_asset_config(dm)
    return (not all_errors), {"errors": all_errors, "maps": details}


def _print_report(result: dict) -> None:
    for m, detail in result["maps"].items():
        print(f"\n  {m}")
        print(f"    {'point':<30} {'addr':>6} {'off':>5} {'type':>8} {'scale':>8} {'rw':>3}")
        for name, fp in detail.items():
            print(
                f"    {name:<30} {fp['address']:>6} {fp['offset']:>5} "
                f"{fp['type']:>8} {fp['scale']:>8} {fp['rw']:>3}"
            )
    if result["errors"]:
        print("\n  PARITY MISMATCHES:")
        for e in result["errors"]:
            print(f"    !! {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="HIL register-parity check")
    ap.add_argument("--json", help="write the full result as JSON to this path")
    ap.add_argument("--quiet", action="store_true", help="only print pass/fail")
    args = ap.parse_args()

    ok, result = run()
    if not args.quiet:
        _print_report(result)
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")

    n_points = sum(len(d) for d in result["maps"].values())
    if ok:
        print(f"\n  PARITY OK: {n_points} points across {len(result['maps'])} maps "
              f"agree (map = simulator = HIL server).")
        return 0
    print(f"\n  PARITY FAILED: {len(result['errors'])} mismatch(es).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
