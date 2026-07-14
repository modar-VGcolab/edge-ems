"""A simulated Modbus TCP device built from a register map file.

The datastore layout comes from the same map file core.py reads, so the
simulator and the real adapter agree by construction (plan task 8). For SunSpec
maps the full walkable image (SunS marker, model headers, scale-factor and
unused-point registers, end model) is initialised verbatim from each register's
`value`, and canonical points are scaled live via their `sf_address`.

pymodbus is pinned >=3.12,<3.13: the 3.13 datastore rewrite (SimData) breaks
zero-based sequential blocks. Revisit the pin when 3.13.x settles.
"""

from __future__ import annotations

from common.modbus_codec import _words_to_int, decode, encode, encode_raw, holding_offset
from common.register_map import RegisterDef, RegisterMap
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import StartAsyncTcpServer

_FX_HOLDING = 3


class SimulatedDevice:
    def __init__(self, rmap: RegisterMap, initial: dict[str, float] | None = None):
        self.rmap = rmap
        size = max(holding_offset(r.address) + r.width for r in rmap.points.values()) + 1
        self._block = ModbusSequentialDataBlock(0, [0] * (size + 2))
        self._device = ModbusDeviceContext(hr=self._block)
        self.context = ModbusServerContext(self._device, single=True)

        # Serve raw image registers verbatim (markers, headers, SF, sentinels).
        for reg in rmap.points.values():
            if reg.value is not None:
                self._device.setValues(
                    _FX_HOLDING, holding_offset(reg.address), encode_raw(reg, reg.value)
                )
        for name, value in (initial or {}).items():
            self.set_point(name, value)

    def _sf(self, reg: RegisterDef) -> int | None:
        if reg.sf_address is None:
            return None
        w = self._device.getValues(_FX_HOLDING, holding_offset(reg.sf_address), 1)
        return _words_to_int("sunssf", list(w))

    def set_point(self, name: str, value: float) -> None:
        reg = self.rmap.points[name]
        self._device.setValues(
            _FX_HOLDING, holding_offset(reg.address), encode(reg, value, sf=self._sf(reg))
        )

    def get_point(self, name: str) -> float:
        reg = self.rmap.points[name]
        words = self._device.getValues(_FX_HOLDING, holding_offset(reg.address), reg.width)
        return decode(reg, list(words), sf=self._sf(reg))

    async def serve(self, host: str = "0.0.0.0", port: int = 15020) -> None:
        await StartAsyncTcpServer(context=self.context, address=(host, port))
