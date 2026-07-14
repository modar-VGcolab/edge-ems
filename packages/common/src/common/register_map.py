"""Register map files: canonical point name -> Modbus register definition.

Maps are the translation layer between the data model and a device's as-built
firmware layout. Adding a device or firmware revision is a new map file, not code.

Two register flavors coexist:
  * canonical points  -- named exactly as a data_model point; the controller
    reads/writes these. May use a legacy folded `scale`, or SunSpec live scaling
    via `sf_address` (read the scale-factor register at runtime) / `sf_value`
    (fixed) plus `unit_scale` (SI -> canonical unit, e.g. W -> kW).
  * raw image registers -- any other register in a SunSpec walkable image
    (SunS marker, model ID/L headers, scale-factor regs, unused model points,
    vendor blocks, end model). These MUST carry a `role` so a typo in a
    canonical name is still rejected. They are served verbatim (`value`) and
    ignored by the controller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from common.data_model import DataModel

_WIDTH = {
    "uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2,
    "enum16": 1, "bitfield32": 2, "uint64": 4, "sunssf": 1,
}
# string/pad take an explicit `size`.


class RegisterDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: int = Field(ge=0, le=65535)
    type: Literal[
        "uint16", "int16", "uint32", "int32", "float32",
        "enum16", "bitfield32", "uint64", "sunssf", "string", "pad",
    ]
    size: int | None = None          # explicit width override (string/pad/multi)
    scale: float = 1.0               # legacy folded scale (non-SunSpec maps)
    sf_address: int | None = None    # SunSpec live scale-factor register address
    sf_value: int | None = None      # fixed scale factor when no live register
    unit_scale: float = 1.0          # SI unit -> canonical unit (W->kW = 0.001)
    value: int | None = None         # served/init value for raw image registers
    role: str | None = None          # marker|header|sf|data|sentinel|vendor|end
    rw: Literal["r", "rw", "w"] = "r"

    @property
    def width(self) -> int:
        if self.size is not None:
            return self.size
        if self.type in ("string", "pad"):
            raise ValueError(f"type '{self.type}' requires an explicit size")
        return _WIDTH[self.type]

    @property
    def sunspec_scaled(self) -> bool:
        return self.sf_address is not None or self.sf_value is not None

    @model_validator(mode="after")
    def _check(self):
        if self.sf_address is not None and self.sf_value is not None:
            raise ValueError("use either sf_address or sf_value, not both")
        return self


class RegisterMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    map_version: str
    asset_class: str
    device: str = ""
    base_address: int = 40001
    points: dict[str, RegisterDef]

    def canonical(self, dm_points: set[str]) -> dict[str, RegisterDef]:
        """The subset the controller cares about: data_model-named points."""
        return {n: r for n, r in self.points.items() if n in dm_points}


def load_register_map(path: str | Path, dm: DataModel) -> RegisterMap:
    """Load and cross-validate a register map. Raises ValueError on any error."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    rmap = RegisterMap.model_validate(raw)
    errors: list[str] = []
    cls_ = dm.asset_classes.get(rmap.asset_class)
    if cls_ is None:
        raise ValueError(f"{path}: unknown asset class '{rmap.asset_class}'")
    occupied: dict[int, str] = {}
    for pname, reg in rmap.points.items():
        point = cls_.points.get(pname)
        if point is None:
            # not a canonical point -> must be an explicitly-roled image register
            if reg.role is None:
                errors.append(
                    f"unknown point '{pname}' for class '{rmap.asset_class}' "
                    f"(canonical points must match data_model; raw image registers need a 'role')"
                )
        else:
            if point.direction == "output" and reg.rw == "r":
                errors.append(f"'{pname}' is an output point but register is read-only")
            if point.direction == "input" and reg.rw == "w":
                errors.append(f"'{pname}' is an input point but register is write-only")
        for offset in range(reg.width):
            addr = reg.address + offset
            if addr in occupied:
                errors.append(
                    f"'{pname}' overlaps register {addr} already used by '{occupied[addr]}'"
                )
            occupied[addr] = pname
    if errors:
        raise ValueError(f"register map {path} invalid: " + "; ".join(errors))
    return rmap
